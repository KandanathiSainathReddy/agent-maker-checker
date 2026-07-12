"""Tool schema shape + strict validation tests (schemas.py). Offline, no Bedrock."""

from __future__ import annotations

from agent.schemas import PROXIED_TOOLS, TOOLS, tool_specs, validate_tool_call

EXPECTED_TOOLS = {"issue_refund", "create_payment_link", "pay_vendor", "get_ticket", "list_orders"}


def test_tool_vocabulary_matches_contracts():
    assert set(TOOLS) == EXPECTED_TOOLS


def test_get_ticket_is_the_only_non_proxied_tool():
    assert "get_ticket" not in PROXIED_TOOLS
    assert set(PROXIED_TOOLS) == EXPECTED_TOOLS - {"get_ticket"}


def test_tool_specs_shape_is_bedrock_converse_ready():
    specs = tool_specs()
    assert len(specs) == len(TOOLS)
    for spec in specs:
        tool_spec = spec["toolSpec"]
        assert tool_spec["name"] in EXPECTED_TOOLS
        assert isinstance(tool_spec["description"], str) and tool_spec["description"]
        schema = tool_spec["inputSchema"]["json"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert isinstance(schema["required"], list)


def test_money_fields_are_integer_typed_in_every_schema():
    for name in ("issue_refund", "create_payment_link", "pay_vendor"):
        props = TOOLS[name]["schema"]["properties"]
        assert props["amount"]["type"] == "integer"


# -- validate_tool_call ------------------------------------------------


def test_valid_issue_refund_passes():
    errors = validate_tool_call(
        "issue_refund", {"payment_id": "pay_x", "amount": 120000, "payee": "cust_ravi@oksbi"}
    )
    assert errors == []


def test_missing_required_field_is_rejected():
    errors = validate_tool_call("issue_refund", {"payment_id": "pay_x", "amount": 120000})
    assert any("payee" in e for e in errors)


def test_wrong_type_is_rejected():
    errors = validate_tool_call(
        "issue_refund", {"payment_id": "pay_x", "amount": "120000", "payee": "cust_ravi@oksbi"}
    )
    assert any("amount" in e and "type" in e for e in errors)


def test_unknown_tool_is_rejected():
    errors = validate_tool_call("delete_all_data", {})
    assert len(errors) == 1
    assert "unknown tool" in errors[0]


def test_additional_properties_are_rejected():
    errors = validate_tool_call(
        "issue_refund",
        {
            "payment_id": "pay_x",
            "amount": 120000,
            "payee": "cust_ravi@oksbi",
            "unexpected_field": "sneaky",
        },
    )
    assert any("unexpected_field" in e for e in errors)


def test_amount_below_minimum_is_rejected():
    errors = validate_tool_call(
        "issue_refund", {"payment_id": "pay_x", "amount": 0, "payee": "cust_ravi@oksbi"}
    )
    assert any("amount" in e for e in errors)


def test_boolean_is_not_accepted_as_integer():
    # bool is a python int subclass; JSON Schema (and our validator) must not
    # accept True/False where an integer amount is required.
    errors = validate_tool_call(
        "issue_refund", {"payment_id": "pay_x", "amount": True, "payee": "cust_ravi@oksbi"}
    )
    assert any("amount" in e for e in errors)


def test_arguments_must_be_an_object():
    errors = validate_tool_call("issue_refund", "not-a-dict")
    assert len(errors) == 1
    assert "JSON object" in errors[0]


def test_list_orders_has_no_required_fields():
    errors = validate_tool_call("list_orders", {})
    assert errors == []


def test_get_ticket_requires_ticket_id():
    errors = validate_tool_call("get_ticket", {})
    assert any("ticket_id" in e for e in errors)
