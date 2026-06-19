"""Tests for authoritative module-variable contract parsing."""

from __future__ import annotations

from iac_smith.nodes.contract import parse_module_variables


def test_required_and_optional_variables_distinguished() -> None:
    variables_tf = (
        'variable "vpc_cidr" {\n  type = string\n}\n\n'
        'variable "environment" {\n  type    = string\n  default = "non-prod"\n}\n'
    )
    result = parse_module_variables(variables_tf)
    assert result == {"vpc_cidr": False, "environment": True}


def test_complex_default_is_detected() -> None:
    variables_tf = (
        'variable "tags" {\n  type    = map(string)\n  default = {\n    a = "b"\n  }\n}\n'
    )
    assert parse_module_variables(variables_tf) == {"tags": True}


def test_default_nested_in_validation_block_is_not_a_default() -> None:
    # A `default` token nested inside another block must not be mistaken for the
    # variable's own default — the variable remains required.
    variables_tf = (
        'variable "name" {\n'
        "  type = string\n"
        "  validation {\n"
        "    condition     = length(var.name) > 0\n"
        '    error_message = "default name not allowed"\n'
        "  }\n"
        "}\n"
    )
    assert parse_module_variables(variables_tf) == {"name": False}


def test_malformed_hcl_falls_back_to_regex() -> None:
    # Unbalanced braces make this invalid HCL; the regex fallback should still
    # recover the variable names and default presence.
    variables_tf = 'variable "a" {\n  type = string\n  default = "x"\n'
    assert parse_module_variables(variables_tf) == {"a": True}


def test_empty_content_returns_empty() -> None:
    assert parse_module_variables("") == {}
