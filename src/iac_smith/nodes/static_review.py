import posixpath
import re
from pathlib import Path

from iac_smith.models.validation import ValidationResult, ValidationStatus
from iac_smith.nodes.contract import parse_module_variables

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY"),
    re.compile(r"aws_(access_key_id|secret_access_key)\s*=", re.IGNORECASE),
    re.compile(r"""(password|token|secret)\s*=\s*(?:"[^"]{6,}"|'[^']{6,}')""", re.IGNORECASE),
]

# Matches both old-style bracket form and newer aws_vpc_security_group_ingress_rule form.
_CIDR_BLOCK_V4 = re.compile(
    r"cidr_blocks\s*=\s*\[[^\]]*0\.0\.0\.0/0"
    r"|cidr_ipv4\s*=\s*[\"']0\.0\.0\.0/0[\"']",
    re.IGNORECASE | re.DOTALL,
)
_CIDR_BLOCK_V6 = re.compile(
    r"ipv6_cidr_blocks\s*=\s*\[[^\]]*::/0"
    r"|cidr_ipv6\s*=\s*[\"']::/0[\"']",
    re.IGNORECASE | re.DOTALL,
)
# Ports where public open ingress is unambiguously dangerous.
_DANGEROUS_PORTS = {22, 3389, 5432, 3306, 1433, 6379, 27017}
_PORT_RE = re.compile(r"(?:from_port|to_port)\s*=\s*(\d+)")
_MODULE_DECL_RE = re.compile(r'\bmodule\s+"([^"]+)"')
_MODULE_REF_RE = re.compile(r"\bmodule\.([A-Za-z0-9_-]+)\.")


def _has_main_or_master_branch_filter(content: str) -> bool:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)branches\s*:\s*(.*)$", line)
        if not match:
            continue

        branch_indent = len(match.group(1))
        inline_value = match.group(2)
        if re.search(r"\b(main|master)\b", inline_value):
            return True

        for child in lines[index + 1 :]:
            if not child.strip():
                continue
            child_indent = len(child) - len(child.lstrip())
            if child_indent <= branch_indent:
                break
            if re.match(r"^\s*-\s*(main|master)\s*(?:#.*)?$", child):
                return True
    return False


def _apply_workflow_errors(path: str, content: str) -> list[str]:
    if path != ".github/workflows/terraform-apply.yml":
        return []

    errors = []
    if re.search(r"^\s*pull_request\s*:", content, re.MULTILINE):
        errors.append(
            f"Terraform apply workflow `{path}` must not run on pull requests or feature branches."
        )
    if not _has_main_or_master_branch_filter(content):
        errors.append(
            f"Terraform apply workflow `{path}` push trigger must be limited to main or master."
        )
    if not re.search(r"^\s*environment\s*:", content, re.MULTILINE):
        errors.append(
            f"Terraform apply workflow `{path}` must gate apply behind a manual approval "
            f"`environment:` so merging to main never auto-applies without sign-off."
        )
    if "needs.detect.outputs" not in content:
        errors.append(
            f"Terraform apply workflow `{path}` must scope the run to changed components "
            f"via a `detect` job (apply jobs gate on `needs.detect.outputs.*`) so an "
            f"unrelated push never re-applies unchanged live infrastructure."
        )
    return errors


def _module_root(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "modules" and path.endswith(".tf"):
        return "/".join(parts[:2])
    return None


def _find_undeclared_module_references(generated_files: dict[str, str]) -> list[str]:
    declared_by_root: dict[str, set[str]] = {}
    referenced_by_root: dict[str, set[str]] = {}

    for path, content in generated_files.items():
        root = _module_root(path)
        if not root:
            continue
        declared_by_root.setdefault(root, set()).update(_MODULE_DECL_RE.findall(content))
        referenced_by_root.setdefault(root, set()).update(_MODULE_REF_RE.findall(content))

    errors = []
    for root, references in sorted(referenced_by_root.items()):
        declared = declared_by_root.get(root, set())
        for module_name in sorted(references - declared):
            errors.append(
                f"Generated module `{root}` references `module.{module_name}` "
                f'but does not declare `module "{module_name}"`.'
            )
    return errors


_VAR_DECL_RE = re.compile(r'\bvariable\s+"([^"]+)"')
_OUTPUT_DECL_RE = re.compile(r'\boutput\s+"([^"]+)"')
_REQUIRED_PROVIDERS_RE = re.compile(r"required_providers\s*{")


def _suggest_keep_file(locations: list[str], preferred: str) -> str:
    """Suggest which file to keep based on Terraform conventions.

    Compares the basename or suffix since locations are full module paths
    like ``modules/ecs-fargate/variables.tf`` and preferred is a short
    name like ``variables.tf``.
    """
    keep = locations[0]
    for loc in locations:
        if loc.endswith(f"/{preferred}") or loc == preferred:
            keep = loc
            break
    remove = [loc for loc in locations if loc != keep]
    if remove:
        return f"Remove from {remove[0]}, keep in {keep}."
    return "Keep one declaration only."


def _find_cross_file_duplicates(generated_files: dict[str, str]) -> list[str]:
    """Detect variable/output/provider declarations duplicated across files in the same module.

    Terraform modules must have unique variable names, output names, and at most one
    required_providers block. If a variable is declared in both ``main.tf`` and
    ``variables.tf`` of the same module, that will fail ``terraform init`` with
    "Duplicate variable declaration". Same for outputs and required_providers.
    """
    var_by_root: dict[str, dict[str, list[str]]] = {}
    output_by_root: dict[str, dict[str, list[str]]] = {}
    prov_by_root: dict[str, list[str]] = {}
    errors = []

    for path, content in generated_files.items():
        root = _module_root(path)
        if not root:
            continue

        for m in _VAR_DECL_RE.finditer(content):
            name = m.group(1)
            var_by_root.setdefault(root, {}).setdefault(name, []).append(path)

        for m in _OUTPUT_DECL_RE.finditer(content):
            name = m.group(1)
            output_by_root.setdefault(root, {}).setdefault(name, []).append(path)

        if _REQUIRED_PROVIDERS_RE.search(content):
            prov_by_root.setdefault(root, []).append(path)

    for root, vars_by_name in sorted(var_by_root.items()):
        for name, locations in sorted(vars_by_name.items()):
            if len(locations) > 1:
                hint = _suggest_keep_file(locations, preferred="variables.tf")
                errors.append(
                    f'Variable "{name}" declared in multiple files of module '
                    f"`{root}`: {', '.join(locations)}. {hint}"
                )

    for root, outputs_by_name in sorted(output_by_root.items()):
        for name, locations in sorted(outputs_by_name.items()):
            if len(locations) > 1:
                hint = _suggest_keep_file(locations, preferred="outputs.tf")
                errors.append(
                    f'Output "{name}" declared in multiple files of module '
                    f"`{root}`: {', '.join(locations)}. {hint}"
                )

    for root, locations in sorted(prov_by_root.items()):
        if len(locations) > 1:
            hint = _suggest_keep_file(locations, preferred="versions.tf")
            errors.append(
                f"required_providers block found in multiple files of module "
                f"`{root}`: {', '.join(locations)}. {hint}"
            )

    return errors


_VAR_REF_RE = re.compile(r"\bvar\.([A-Za-z0-9_-]+)\b")


def _find_undeclared_variable_references(generated_files: dict[str, str]) -> list[str]:
    """Detect ``var.xxx`` references in module files that lack a ``variable "xxx"`` declaration.

    Bedrock sometimes references variables in ``main.tf`` without declaring them in
    ``variables.tf``.  When a module has a ``variables.tf``, only declarations in
    that file count as valid — declarations in ``main.tf`` alone are treated as
    undeclared because they violate module file-organization rules and get removed
    when ``main.tf`` is repaired.  For modules without a ``variables.tf``, all
    declaration sites are accepted.
    """
    refs_by_root: dict[str, dict[str, list[str]]] = {}
    all_decls_by_root: dict[str, set[str]] = {}
    vars_tf_decls_by_root: dict[str, set[str]] = {}
    roots_with_vars_tf: set[str] = set()
    errors = []

    for path, content in generated_files.items():
        root = _module_root(path)
        if not root:
            continue

        is_vars_tf = path.endswith("/variables.tf")
        if is_vars_tf:
            roots_with_vars_tf.add(root)

        for m in _VAR_REF_RE.finditer(content):
            refs_by_root.setdefault(root, {}).setdefault(m.group(1), []).append(path)

        for m in _VAR_DECL_RE.finditer(content):
            name = m.group(1)
            all_decls_by_root.setdefault(root, set()).add(name)
            if is_vars_tf:
                vars_tf_decls_by_root.setdefault(root, set()).add(name)

    for root, var_refs in sorted(refs_by_root.items()):
        if root in roots_with_vars_tf:
            declared = vars_tf_decls_by_root.get(root, set())
        else:
            declared = all_decls_by_root.get(root, set())
        for name in sorted(var_refs):
            if name not in declared:
                locations = sorted(set(var_refs[name]))
                variables_tf = f"{root}/variables.tf"
                errors.append(
                    f"var.{name} is referenced in {', '.join(locations)} "
                    f'but "{name}" is not declared in {variables_tf}. '
                    f'Add variable "{name}" to {variables_tf}.'
                )

    return errors


_TG_LOCALS_HEADER_RE = re.compile(r"\blocals\s*\{")
_TG_INPUTS_HEADER_RE = re.compile(r"\binputs\s*=\s*\{")
_TG_INCLUDE_BLOCK_RE = re.compile(r"^\s*include\s*(?:\"[^\"]+\"\s*)?\{", re.MULTILINE)
_TG_LOCAL_REF_RE = re.compile(r"\blocal\.([A-Za-z0-9_]+)")
_TG_SOURCE_RE = re.compile(r'source\s*=\s*"([^"]+)"')
_REDACTED_PLACEHOLDER_RE = re.compile(r"\*{3}")
_SINGLETON_RESOURCE_TYPES = frozenset({"aws_vpc"})
_RESOURCE_TYPE_RE = re.compile(r'\bresource\s+"([^"]+)"')
_RESOURCE_BLOCK_RE = re.compile(r'\bresource\s+"([^"]+)"\s+"([^"]+)"\s*\{')
_NAME_ATTR_RE = re.compile(r'^\s*name\s*=\s*"([^"]+)"', re.MULTILINE)
_NAMED_RESOURCE_TYPES = frozenset(
    {
        "aws_cloudwatch_log_group",
        "aws_ecs_cluster",
        "aws_iam_role",
        "aws_lb",
        "aws_lb_target_group",
        "aws_security_group",
    }
)
_DEPENDENCY_HEADER_RE = re.compile(r'\bdependency\s+"([^"]+)"\s*\{')
_CONFIG_PATH_RE = re.compile(r'\bconfig_path\s*=\s*"([^"]+)"')
_TG_DEPENDENCY_OUTPUT_REF_RE = re.compile(
    r"\bdependency\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)\b"
)


def _strip_hcl_comments(content: str) -> str:
    """Remove HCL comments while preserving quoted strings and line numbers."""
    result: list[str] = []
    in_block_comment = False
    in_string = False
    escape = False
    i = 0
    while i < len(content):
        ch = content[i]
        nxt = content[i + 1] if i + 1 < len(content) else ""

        if in_block_comment:
            if ch == "\n":
                result.append("\n")
            elif ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
            else:
                result.append(" ")
            i += 1
            continue

        if in_string:
            result.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            result.append(ch)
        elif ch == "#" or ch == "/" and nxt == "/":
            while i < len(content) and content[i] != "\n":
                result.append(" ")
                i += 1
            continue
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            result.extend("  ")
            i += 1
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def _extract_hcl_block_keys(content: str, header_re: re.Pattern) -> set[str]:
    """Return top-level assignment keys inside the first matching HCL block.

    This intentionally ignores nested object keys. For example, in
    `inputs = { tags = { Environment = local.environment } }`, only `tags` is a
    Terragrunt input; `Environment` is just a map key inside that input value.
    """
    m = header_re.search(content)
    if not m:
        return set()
    block_body = _extract_hcl_block_body(content, m.start())
    if block_body is None:
        return set()

    keys: set[str] = set()
    nested_depth = 0
    for line in block_body.splitlines():
        if nested_depth == 0:
            km = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
            if km:
                keys.add(km.group(1))
        nested_depth += line.count("{") - line.count("}")
        if nested_depth < 0:
            nested_depth = 0
    return keys


def _extract_hcl_block_body(content: str, start_pos: int) -> str | None:
    brace_pos = content.find("{", start_pos)
    if brace_pos == -1:
        return None

    depth = 0
    for i in range(brace_pos, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return content[brace_pos + 1 : i]
    return None


def _extract_named_hcl_blocks(content: str, header_re: re.Pattern) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in header_re.finditer(content):
        body = _extract_hcl_block_body(content, match.start())
        if body is not None:
            blocks[match.group(1)] = body
    return blocks


def _module_name_from_tg_source(source: str) -> str | None:
    if any(prefix in source for prefix in ("git::", "github.com", "registry.")):
        return None
    m = re.search(r"modules/+([A-Za-z0-9_-]+)", source)
    return m.group(1) if m else None


def _is_stack_terragrunt(path: str) -> bool:
    parts = path.split("/")
    return len(parts) >= 4 and parts[0] == "environments" and parts[-1] == "terragrunt.hcl"


def _find_redacted_placeholders(generated_files: dict[str, str]) -> list[str]:
    """Flag `***` redaction artifacts in generated workflow YAML files.

    When the model sees a token in its context that was redacted by the caller,
    it sometimes reproduces the literal *** placeholder in generated workflow steps.
    That string is not a valid shell expression and will break the workflow run.
    """
    errors = []
    for path, content in generated_files.items():
        if not path.endswith((".yml", ".yaml")):
            continue
        if _REDACTED_PLACEHOLDER_RE.search(content):
            errors.append(
                f"Workflow `{path}` contains a literal `***` redaction artifact. "
                "Replace with a real GitHub Actions expression such as "
                "`${{ github.token }}` or `${{ secrets.MY_TOKEN }}`."
            )
    return errors


def _find_terragrunt_orphaned_locals(generated_files: dict[str, str]) -> list[str]:
    """Flag `local.<name>` references in child Terragrunt configs where `<name>` is
    not declared in the same file's `locals {}` block.

    In Terragrunt, including a parent config does NOT merge the parent's locals into
    `local.*`. A child can only access parent locals via `include.<label>.locals.<name>`
    when the include block sets `expose = true`. References to `local.env` etc. that
    are only defined in a parent will cause `Error: Unsupported attribute` at init time.
    """
    errors = []
    for path, content in generated_files.items():
        if not path.endswith("terragrunt.hcl"):
            continue
        if not _TG_INCLUDE_BLOCK_RE.search(content):
            continue
        local_keys = _extract_hcl_block_keys(content, _TG_LOCALS_HEADER_RE)
        content_without_comments = _strip_hcl_comments(content)
        seen: set[str] = set()
        for m in _TG_LOCAL_REF_RE.finditer(content_without_comments):
            name = m.group(1)
            if name in local_keys or name in seen:
                continue
            seen.add(name)
            errors.append(
                f"Terragrunt config `{path}` references `local.{name}` "
                f"which is not declared in this file's `locals {{}}` block. "
                f"Parent locals are not available as `local.*` in child configs; "
                f"declare `{name}` locally or use "
                f"`include.<label>.locals.{name}` with `expose = true`."
            )
    return errors


def _find_terragrunt_required_providers(generated_files: dict[str, str]) -> list[str]:
    """Flag `required_providers` declared inside any terragrunt.hcl.

    `required_providers` belongs ONLY in a module's `versions.tf`. When a
    Terragrunt `generate` block emits a `provider.tf` that also declares
    `required_providers`, `terraform init` fails with "Duplicate required
    providers configuration" against the module's `versions.tf`. That generated
    `provider.tf` is not a planned file, so module-level duplicate checks never
    see it and the collision only surfaces deep in the runtime plan. Catch it
    here: a terragrunt.hcl must never contain `required_providers`.
    """
    errors = []
    for path, content in generated_files.items():
        if not path.endswith("terragrunt.hcl"):
            continue
        if _REQUIRED_PROVIDERS_RE.search(content):
            errors.append(
                f"Terragrunt config `{path}` declares `required_providers` (likely inside a "
                f"`generate` block). required_providers belongs ONLY in a module's "
                f"`versions.tf`; a generated `provider.tf` that also declares it collides "
                f'with `versions.tf` at `terraform init` ("Duplicate required providers '
                f'configuration"). Make the `generate` block emit only a `provider "aws"` '
                f"block — no `terraform {{}}` / `required_providers`."
            )
    return errors


def _find_terragrunt_missing_required_inputs(generated_files: dict[str, str]) -> list[str]:
    """Flag required module variables that the Terragrunt stack does not pass.

    This is the one input/variable rule that is a *real* error: a variable with
    no `default =` that the live Terragrunt stack never provides will fail
    `terragrunt plan/apply` in non-interactive mode. The reverse — a stack
    passing an input the module does not declare — is NOT an error: Terragrunt
    passes inputs as `TF_VAR_*` environment variables and Terraform silently
    ignores undeclared ones, so it is not checked here.

    The module's `variables.tf`, parsed authoritatively via
    `parse_module_variables`, is the single source of truth for which variables
    are required.
    """
    errors = []
    for path, content in generated_files.items():
        if not _is_stack_terragrunt(path):
            continue
        source_m = _TG_SOURCE_RE.search(content)
        if not source_m:
            continue
        module_name = _module_name_from_tg_source(source_m.group(1))
        if not module_name:
            continue
        vars_tf_path = f"modules/{module_name}/variables.tf"
        vars_content = generated_files.get(vars_tf_path)
        if vars_content is None:
            continue

        required_vars = {
            name
            for name, has_default in parse_module_variables(vars_content).items()
            if not has_default
        }
        input_keys = _extract_hcl_block_keys(content, _TG_INPUTS_HEADER_RE)
        for name in sorted(required_vars - input_keys):
            errors.append(
                f"Terragrunt stack `{path}` does not pass required input `{name}` "
                f'declared in `{vars_tf_path}` as `variable "{name}"` without a default. '
                f"Add `{name}` to the stack `inputs = {{}}` block or add a safe default."
            )
    return errors


def _dependency_module_outputs_path(config_path: str) -> str | None:
    # Most generated Terragrunt stacks use `config_path = "../foundation"`; map the
    # referenced stack directory to the matching generated `modules/<stack>/outputs.tf`.
    clean = config_path.rstrip("/")
    if not clean or clean.startswith(("git::", "http://", "https://")):
        return None
    stack_name = clean.split("/")[-1]
    if not stack_name or stack_name in {".", ".."}:
        return None
    return f"modules/{stack_name}/outputs.tf"


def _find_terragrunt_dependency_output_mismatches(
    generated_files: dict[str, str],
) -> list[str]:
    """Flag `dependency.<name>.outputs.foo` refs missing from dependency outputs.tf."""
    errors = []
    for path, content in generated_files.items():
        if not _is_stack_terragrunt(path):
            continue

        dependency_outputs_paths: dict[str, str] = {}
        for label, block in _extract_named_hcl_blocks(content, _DEPENDENCY_HEADER_RE).items():
            config_m = _CONFIG_PATH_RE.search(block)
            if not config_m:
                continue
            outputs_path = _dependency_module_outputs_path(config_m.group(1))
            if outputs_path is not None:
                dependency_outputs_paths[label] = outputs_path

        refs_by_label: dict[str, set[str]] = {}
        for match in _TG_DEPENDENCY_OUTPUT_REF_RE.finditer(content):
            label, output_name = match.groups()
            refs_by_label.setdefault(label, set()).add(output_name)

        for label, output_names in sorted(refs_by_label.items()):
            outputs_path = dependency_outputs_paths.get(label)
            if outputs_path is None:
                continue
            outputs_content = generated_files.get(outputs_path)
            if outputs_content is None:
                continue
            declared_outputs = {m.group(1) for m in _OUTPUT_DECL_RE.finditer(outputs_content)}
            for output_name in sorted(output_names - declared_outputs):
                errors.append(
                    f"Terragrunt stack `{path}` references "
                    f"`dependency.{label}.outputs.{output_name}` but "
                    f'`{outputs_path}` has no `output "{output_name}"` declaration. '
                    f"Add the output to `{outputs_path}` or update `{path}` "
                    f"to use an existing output name."
                )
    return errors


def existing_stack_dirs(repo_path: Path | str | None) -> set[str]:
    """Repo-relative directories of every Terragrunt stack already in the target repo.

    Used so a workload that legitimately depends on a pre-existing foundation
    stack (one this change does not regenerate) is not flagged as dangling.
    """
    if not repo_path:
        return set()
    root = Path(repo_path)
    env_dir = root / "environments"
    if not env_dir.is_dir():
        return set()
    dirs: set[str] = set()
    for tg in env_dir.rglob("terragrunt.hcl"):
        rel = tg.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        rel_posix = rel.as_posix()
        if _is_stack_terragrunt(rel_posix):
            dirs.add(posixpath.dirname(rel_posix))
    return dirs


def _resolve_dependency_target(stack_path: str, config_path: str) -> str | None:
    """Resolve a Terragrunt `config_path` to the repo-relative stack dir it targets.

    Returns ``None`` when the path is remote/unverifiable (git/http) or escapes the
    repo root, so those are left to runtime rather than guessed at here.
    """
    clean = config_path.strip().rstrip("/")
    if not clean or clean.startswith(("git::", "http://", "https://", "github.com")):
        return None
    target = posixpath.normpath(posixpath.join(posixpath.dirname(stack_path), clean))
    if target.startswith("..") or target in (".", ""):
        return None
    return target


def _find_terragrunt_dangling_dependencies(
    generated_files: dict[str, str], known_stack_dirs: set[str]
) -> list[str]:
    """Flag Terragrunt `dependency` references whose target stack does not exist.

    Terragrunt only reports these cryptically, and far too late — at `terragrunt
    plan` ("There is no variable named dependency" / "<stack> does not exist"),
    after the file set is fixed and the runtime repair loop can no longer add the
    missing stack. Two cases:

    1. `dependency.<name>.outputs.*` is referenced but no `dependency "<name>"`
       block is declared in the stack.
    2. A `dependency "<name>"` points at a stack that is neither created by this
       change nor already present in the target repo.

    Both mean the model invented a cross-stack dependency on infrastructure that
    isn't there. Generic by design: the rule is "the target stack must exist",
    never "it must be called foundation". ``known_stack_dirs`` carries the stacks
    that already exist in the repo so pre-existing dependencies are not flagged.
    """
    resolvable = {
        posixpath.dirname(path) for path in generated_files if _is_stack_terragrunt(path)
    } | known_stack_dirs
    errors: list[str] = []
    for path, content in generated_files.items():
        if not _is_stack_terragrunt(path):
            continue
        declared = {
            label: _CONFIG_PATH_RE.search(block)
            for label, block in _extract_named_hcl_blocks(content, _DEPENDENCY_HEADER_RE).items()
        }
        referenced = {
            m.group(1) for m in _TG_DEPENDENCY_OUTPUT_REF_RE.finditer(_strip_hcl_comments(content))
        }
        for label in sorted(referenced):
            if label not in declared:
                errors.append(
                    f"Terragrunt stack `{path}` references `dependency.{label}.outputs.*` "
                    f'but declares no `dependency "{label}"` block. Add the dependency block '
                    f"(with `config_path` and `mock_outputs`) or stop referencing it."
                )
                continue
            config_m = declared[label]
            if config_m is None:
                errors.append(
                    f'Terragrunt stack `{path}` declares `dependency "{label}"` without a '
                    f"`config_path`. Point it at the stack that produces those outputs."
                )
                continue
            target = _resolve_dependency_target(path, config_m.group(1))
            if target is None or target in resolvable:
                continue
            errors.append(
                f"Terragrunt stack `{path}` depends on stack `{target}` (via "
                f'`dependency "{label}"`, `config_path = "{config_m.group(1)}"`), but that '
                f"stack is neither created by this change nor present in the target repo. "
                f"Either create the `{target}` stack in this change, provision those "
                f"resources inside this module, or source them with Terraform data sources "
                f"instead of a cross-stack dependency."
            )
    return errors


def _find_singleton_resource_duplication(generated_files: dict[str, str]) -> list[str]:
    """Flag foundational resource types (e.g. aws_vpc) declared in multiple modules.

    Each foundational resource should be owned by exactly one module (typically `foundation`).
    All other modules must consume its outputs via Terragrunt dependency blocks rather than
    creating their own copy, which would produce duplicate infrastructure and naming conflicts.
    """
    type_to_modules: dict[str, list[str]] = {}
    for path, content in generated_files.items():
        root = _module_root(path)
        if not root or not path.endswith(".tf"):
            continue
        for m in _RESOURCE_TYPE_RE.finditer(content):
            rtype = m.group(1)
            if rtype not in _SINGLETON_RESOURCE_TYPES:
                continue
            bucket = type_to_modules.setdefault(rtype, [])
            if root not in bucket:
                bucket.append(root)
    warnings = []
    for rtype, modules in sorted(type_to_modules.items()):
        if len(modules) > 1:
            mods = ", ".join(f"`{mod}`" for mod in sorted(modules))
            warnings.append(
                f"Resource `{rtype}` is declared in multiple modules: {mods}. "
                f"Only the foundation/networking module should create this resource; "
                f"other modules must consume it via Terragrunt `dependency` outputs."
            )
    return warnings


def _find_duplicate_named_resources(generated_files: dict[str, str]) -> list[str]:
    """Flag same-type AWS resources that use the same provider-level name.

    Terraform validates each module in isolation, so it will not catch a generated
    foundation module and workload module both declaring an AWS object with the
    same name. Several AWS resource names are unique within a VPC, account, or
    region. In generated multi-module PRs, matching names across module roots are
    strong evidence that a shared primitive was emitted twice instead of being
    passed through dependency outputs.
    """
    resources_by_identity: dict[tuple[str, str], list[str]] = {}
    for path, content in generated_files.items():
        root = _module_root(path)
        if not root or not path.endswith(".tf"):
            continue
        for match in _RESOURCE_BLOCK_RE.finditer(content):
            resource_type, resource_name = match.groups()
            if resource_type not in _NAMED_RESOURCE_TYPES:
                continue
            body = _extract_hcl_block_body(content, match.start())
            if body is None:
                continue
            name_match = _NAME_ATTR_RE.search(body)
            if not name_match:
                continue
            provider_name = name_match.group(1)
            resources_by_identity.setdefault((resource_type, provider_name), []).append(
                f"{path}::{resource_type}.{resource_name}"
            )

    errors = []
    for (resource_type, provider_name), locations in sorted(resources_by_identity.items()):
        roots = {location.split("/", 2)[1] for location in locations}
        if len(roots) <= 1:
            continue
        errors.append(
            f"Resource `{resource_type}` uses duplicate provider name "
            f"`{provider_name}` across modules: {', '.join(locations)}. "
            "Move the shared resource to one owner module and pass its ID through "
            "Terragrunt dependency outputs, or give genuinely separate resources "
            "distinct names."
        )
    return errors


def _contains_dangerous_public_ingress(content: str) -> bool:
    has_public_cidr = _CIDR_BLOCK_V4.search(content) or _CIDR_BLOCK_V6.search(content)
    if not has_public_cidr:
        return False
    ports = {int(m.group(1)) for m in _PORT_RE.finditer(content)}
    return bool(ports & _DANGEROUS_PORTS)


# Human-readable description of each blocking security/safety check this review
# enforces. Surfaced in the PR body so reviewers see exactly what was verified
# rather than a single opaque "passed" line. Keep in sync with the error-tier
# checks below.
_SECURITY_CHECKS_PERFORMED = (
    "No hardcoded secrets or AWS credentials (access keys, private keys, "
    "password/token/secret literals) in any generated file.",
    "No `***` redaction artifacts left in generated workflow files.",
    "Apply workflow runs only on push to `main` — never on pull requests or feature branches.",
    "Apply is gated behind a manual-approval `environment:` and scoped to the "
    "components that actually changed, so merging never auto-applies unrelated infra.",
    "Terragrunt remote-state keys are namespaced with `path_relative_to_include()` "
    "so stacks cannot overwrite each other's state.",
    "No duplicate provider-level resource names across modules (prevents "
    "apply-time collisions Terraform's per-module validation cannot catch).",
)


def static_review_generated_files(
    generated_files: dict[str, str], known_stack_dirs: set[str] | None = None
) -> ValidationResult:
    # Three tiers:
    #   errors      — security/safety. These BLOCK PR creation; real Terraform
    #                 will not catch them (secrets, workflow privilege, hardcoded
    #                 state keys, redaction artifacts that break workflows).
    #   structural  — semantic correctness (undeclared refs, duplicate decls,
    #                 missing required inputs, dependency output mismatches).
    #                 These do NOT block: they feed the bounded autofix loop and
    #                 are surfaced for review, while the real terraform/terragrunt
    #                 validation in cli.py is the authoritative correctness gate.
    #   warnings    — advisory only (public ingress, missing docs markers,
    #                 singleton-resource duplication). Surfaced, never block.
    errors: list[str] = []
    structural: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    for path, content in generated_files.items():
        errors.extend(_apply_workflow_errors(path, content))

        if not path.endswith(".md"):
            for pattern in SECRET_PATTERNS:
                if pattern.search(content):
                    errors.append(f"Potential hardcoded secret detected in `{path}`.")
                    break

        if path.endswith("terragrunt.hcl") and "key" in content:
            has_remote_state = "remote_state" in content or "terraform.tfstate" in content
            if has_remote_state and "path_relative_to_include()" not in content:
                errors.append(
                    f"Terragrunt state key in `{path}` must use path_relative_to_include()."
                )

        if _contains_dangerous_public_ingress(content):
            warnings.append(f"Dangerous public ingress detected in `{path}` for PR review.")

        if (
            path.startswith("modules/")
            and path.endswith("README.md")
            and ("<!-- BEGIN_TF_DOCS -->" not in content or "<!-- END_TF_DOCS -->" not in content)
        ):
            warnings.append(f"Module README `{path}` is missing terraform-docs markers.")

    # Security/safety — blocking.
    errors.extend(_find_redacted_placeholders(generated_files))

    # Structural/semantic — advisory + autofix, never blocking.
    structural.extend(_find_undeclared_module_references(generated_files))
    structural.extend(_find_cross_file_duplicates(generated_files))
    structural.extend(_find_undeclared_variable_references(generated_files))
    structural.extend(_find_terragrunt_orphaned_locals(generated_files))
    structural.extend(_find_terragrunt_missing_required_inputs(generated_files))
    structural.extend(_find_terragrunt_dependency_output_mismatches(generated_files))
    structural.extend(
        _find_terragrunt_dangling_dependencies(generated_files, known_stack_dirs or set())
    )

    # Cross-module provider name collisions are not caught by module-level
    # terraform validate and can fail only at apply time, so they block PR
    # creation if the repair loops cannot remove them.
    errors.extend(_find_duplicate_named_resources(generated_files))

    # required_providers inside a terragrunt.hcl generate block collides with the
    # module versions.tf at `terraform init`; block so it is caught at review time
    # rather than deep in the runtime plan.
    errors.extend(_find_terragrunt_required_providers(generated_files))

    # Advisory only.
    warnings.extend(_find_singleton_resource_duplication(generated_files))

    if errors:
        status = ValidationStatus.FAILED
    elif warnings or structural:
        status = ValidationStatus.PARTIAL
    else:
        status = ValidationStatus.PASSED

    if not errors:
        checks.extend(_SECURITY_CHECKS_PERFORMED)

    return ValidationResult(
        status=status,
        checks=checks,
        warnings=warnings,
        errors=errors,
        structural=structural,
    )
