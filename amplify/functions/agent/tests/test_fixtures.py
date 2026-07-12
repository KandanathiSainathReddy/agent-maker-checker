"""Ticket fixture sanity checks. Offline, no Bedrock."""

from __future__ import annotations

from agent.tickets import list_ticket_ids, load_ticket, untrusted_text

REQUIRED_FIELDS = {"id", "subject", "customer_email", "order_id", "status", "body"}


def test_ticket_4471_hides_the_attacker_handle_in_its_body():
    ticket = load_ticket("4471")
    assert ticket is not None
    assert "attacker@ybl" in ticket["body"]
    # The indirect-injection scene requires this NOT to be in the operator's
    # own task text — it must only ever surface via get_ticket.
    assert "attacker@ybl" not in ticket.get("subject", "")


def test_ticket_4471_reads_as_an_urgent_embedded_instruction():
    ticket = load_ticket("4471")
    body_lower = ticket["body"].lower()
    assert "urgent" in body_lower
    assert "refund" in body_lower


def test_at_least_three_benign_tickets_exist_alongside_4471():
    ids = list_ticket_ids()
    assert "4471" in ids
    benign = [i for i in ids if i != "4471"]
    assert len(benign) >= 3


def test_benign_tickets_do_not_contain_the_attacker_handle():
    for ticket_id in list_ticket_ids():
        if ticket_id == "4471":
            continue
        ticket = load_ticket(ticket_id)
        assert "attacker@ybl" not in ticket["body"]


def test_every_ticket_fixture_has_the_required_fields_and_matching_id():
    for ticket_id in list_ticket_ids():
        ticket = load_ticket(ticket_id)
        assert REQUIRED_FIELDS.issubset(ticket.keys())
        assert ticket["id"] == ticket_id


def test_missing_ticket_returns_none():
    assert load_ticket("does-not-exist") is None


def test_untrusted_text_combines_subject_and_body():
    ticket = load_ticket("4471")
    text = untrusted_text(ticket)
    assert ticket["subject"] in text
    assert ticket["body"] in text
