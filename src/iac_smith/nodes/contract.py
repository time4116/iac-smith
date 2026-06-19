"""Authoritative parsing of a Terraform module's variable contract.

A module's ``variables.tf`` is the single source of truth for which variables a
module accepts and which are required (no ``default``).  Static review uses this
to enforce the one Terragrunt-input rule that is a *real* error — a required
variable the stack fails to pass — while ignoring extra inputs, which Terraform
silently drops (Terragrunt passes inputs as ``TF_VAR_*`` environment variables,
and Terraform ignores undeclared ones).

Parsing uses the real HCL parser (``python-hcl2``) so default-detection is
correct even for complex/multi-line types, with a regex fallback for the
partially-malformed output a generator can emit mid-repair.
"""

import re

import hcl2

_VAR_DECL_RE = re.compile(r'\bvariable\s+"([^"]+)"')


def _strip_quotes(name: str) -> str:
    # python-hcl2 (4.x) returns block labels with their surrounding quotes, e.g.
    # the key for `variable "vpc_cidr"` is the literal string `"vpc_cidr"`.
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name[1:-1]
    return name


def parse_module_variables(variables_tf: str) -> dict[str, bool]:
    """Return ``{variable_name: has_default}`` for a module's ``variables.tf``.

    ``has_default`` is True when the variable declares a ``default`` (making it
    optional for callers).  Falls back to regex scanning if the content does not
    parse as valid HCL.
    """
    try:
        parsed = hcl2.loads(variables_tf)
    except Exception:
        return _parse_module_variables_regex(variables_tf)

    result: dict[str, bool] = {}
    for block in parsed.get("variable", []):
        if not isinstance(block, dict):
            continue
        for raw_name, body in block.items():
            name = _strip_quotes(raw_name)
            result[name] = isinstance(body, dict) and "default" in body
    return result


def _parse_module_variables_regex(variables_tf: str) -> dict[str, bool]:
    """Best-effort fallback when ``variables.tf`` is not valid HCL.

    Detects a ``default`` assignment at the top level of each variable block by
    scanning brace depth, so a ``default`` key nested inside a ``validation`` or
    ``type`` expression is not mistaken for the variable's own default.
    """
    result: dict[str, bool] = {}
    for match in _VAR_DECL_RE.finditer(variables_tf):
        name = match.group(1)
        brace_pos = variables_tf.find("{", match.end())
        if brace_pos == -1:
            result[name] = False
            continue
        depth = 0
        body_start = brace_pos
        body_end = len(variables_tf)
        for i in range(brace_pos, len(variables_tf)):
            if variables_tf[i] == "{":
                depth += 1
            elif variables_tf[i] == "}":
                depth -= 1
                if depth == 0:
                    body_end = i
                    break
        body = variables_tf[body_start + 1 : body_end]
        has_default = False
        nested = 0
        for line in body.splitlines():
            if nested == 0 and re.match(r"^\s*default\s*=", line):
                has_default = True
                break
            nested += line.count("{") - line.count("}")
            if nested < 0:
                nested = 0
        result[name] = has_default
    return result
