"""NetSuite's status sync-back and payment write-back against fake-erp
(decisions §536).

fake-erp's app is loaded in process as the adapter's transport (the same
handlers ``pnpm test:erp`` runs), so the bill push, the vendorPayment
write-back, the ``eid:`` lookup, the delete and the SuiteQL reads of what is
applied to a bill all go through one model of NetSuite. That model encodes
OUR reading of SuiteTalk REST and SuiteQL; the shapes are not yet verified
against a NetSuite account (#654).
"""

# ruff: noqa: F811 — `fake` is the NetSuite adapter tests' fixture, imported to be requested
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from app.services.erp_adapters.base import ErpBillState, PaymentPayload
from app.services.erp_adapters.dispatcher import status_sync_adapter_keys
from app.services.erp_adapters.netsuite import NetSuiteAdapter, NetSuiteStatusReadError
from tests.test_erp_netsuite_adapter import (  # noqa: F401 — `fake` is a fixture
    _REAL_ASYNC_CLIENT,
    _adapter,
    _payload,
    _run,
    _set_status,
    fake,
)

CORRELATION = "5f0d3c1e-8a7b-4c2d-9e10-6b5a4f3e2d1c"


def _push(fake) -> str:
    result = _run(_adapter().post_invoice(_payload()))
    assert result.success, result.message
    return result.erp_document_id


def _outside(fake, bill_id: str, txn_type: str, amount: str, day: str = "2026-11-20") -> str:
    async def go():
        async with _REAL_ASYNC_CLIENT(
            transport=httpx.ASGITransport(app=fake.app), base_url="http://fake-erp.test"
        ) as client:
            resp = await client.post(
                "/__netsuite/apply",
                json={"bill": bill_id, "type": txn_type, "amount": amount, "tranDate": day},
            )
            assert resp.status_code == 200, resp.text
            return resp.json()["id"]

    return _run(go())


def _pay(bill_id: str, amount: str = "100.00", **kw) -> PaymentPayload:
    base = dict(
        payment_id=kw.pop("payment_id", str(uuid.UUID(int=7))),
        bill_id=bill_id,
        amount=Decimal(amount),
        currency="USD",
        paid_on=date(2026, 11, 21),
        pay_from_account_erp_id="121",
    )
    base.update(kw)
    return PaymentPayload(**base)


def _report(bill_id: str):
    return _run(_adapter().bill_report(bill_id))


def test_netsuite_reports_its_bills_back():
    assert NetSuiteAdapter.supports_status_sync is True
    assert NetSuiteAdapter.bills_match_by_reference is False
    assert "netsuite" in status_sync_adapter_keys()


@pytest.mark.parametrize(
    ("value", "on"),
    [
        (None, False),
        ("off", False),
        ("", False),
        ("ON", False),
        ("true", False),
        (1, False),
        ("on", True),
        (True, True),
    ],
)
def test_status_sync_is_on_only_for_a_tenant_that_opted_in(value, on):
    """The capability is the class's; whether a tenant's connection uses it is
    its own opt-in (decisions §541), read by the one effective predicate."""
    from app.services.erp_adapters.base import reports_status_back

    config = {"account_id": "1234567"}
    if value is not None:
        config["netsuite_status_sync"] = value
    adapter = NetSuiteAdapter(config)
    assert adapter.status_sync_on() is on
    assert reports_status_back(adapter) is on


def test_an_open_bill_reads_open_with_our_marker(fake):
    bill = _push(fake)
    report = _report(bill)
    assert report.state is ErpBillState.open
    assert report.correlation_id == CORRELATION
    assert report.paid_amount is None and not report.reduced_outside


def test_a_bill_a_person_paid_in_cash_states_the_exact_cash(fake):
    bill = _push(fake)
    payment = _outside(fake, bill, "VendPymt", "100.00", "2026-11-19")
    report = _report(bill)
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and not report.settled_by_credit
    assert report.paid_amount == Decimal("100.00") and report.paid_currency == "USD"
    assert report.external_payment_ids == (payment,)
    assert report.paid_on == date(2026, 11, 19)
    assert not report.our_payment_applied


def test_a_part_paid_bill_reads_partially_paid_and_states_no_cash(fake):
    bill = _push(fake)
    _outside(fake, bill, "VendPymt", "40.00")
    report = _report(bill)
    assert report.state is ErpBillState.partially_paid
    assert report.paid_amount is None


def test_a_bill_settled_by_a_vendor_credit_is_not_cash(fake):
    bill = _push(fake)
    _outside(fake, bill, "VendCred", "100.00")
    report = _report(bill)
    assert report.state is ErpBillState.paid
    assert report.settled_by_credit and report.paid_amount is None


def test_part_cash_part_credit_is_settled_by_credit(fake):
    bill = _push(fake)
    _outside(fake, bill, "VendPymt", "60.00")
    _outside(fake, bill, "VendCred", "40.00")
    report = _report(bill)
    assert report.state is ErpBillState.paid and report.settled_by_credit
    assert report.paid_amount is None


@pytest.mark.parametrize("status", ["voided", "cancelled"])
def test_a_voided_or_cancelled_bill_reads_deleted(fake, status):
    bill = _push(fake)
    _set_status(fake, bill, status)
    assert _report(bill).state is ErpBillState.deleted


def test_a_bill_netsuite_no_longer_has_reads_deleted(fake):
    assert _report("99999").state is ErpBillState.deleted


def test_a_pending_approval_bill_is_never_open(fake):
    bill = _push(fake)
    _set_status(fake, bill, "pendingApproval")
    assert _report(bill).state is ErpBillState.unknown


def test_a_paid_in_full_status_nothing_covers_reads_unknown(fake):
    bill = _push(fake)
    _set_status(fake, bill, "paidInFull")
    assert _report(bill).state is ErpBillState.unknown


def test_an_unreadable_bill_raises_and_never_reads_gone(fake, monkeypatch):
    bill = _push(fake)

    async def refused(*_a, **_k):
        return None

    monkeypatch.setattr(NetSuiteAdapter, "_suiteql_facts", refused)
    with pytest.raises(NetSuiteStatusReadError):
        _report(bill)


def test_changed_bills_reads_the_keyset_poll(fake):
    bill = _push(fake)
    _outside(fake, bill, "VendPymt", "10.00")
    changed = _run(_adapter().changed_bills(datetime(2026, 11, 24, tzinfo=UTC)))
    assert changed == [bill]


def test_a_failed_change_read_raises_so_the_mark_stays(fake, monkeypatch):
    from app.services.erp_adapters.base import ErpListingIncomplete, ListingGap

    async def failed(*_a, **_k):
        raise ErpListingIncomplete("NetSuite", "vendorBill", ListingGap.fetch_failed)

    monkeypatch.setattr(NetSuiteAdapter, "_suiteql", failed)
    with pytest.raises(NetSuiteStatusReadError):
        _run(_adapter().changed_bills(datetime(2026, 11, 24, tzinfo=UTC)))


# ---------------------------------------------------------------------------
# the write-back
# ---------------------------------------------------------------------------


def test_post_payment_applies_the_exact_amount_to_the_bill(fake):
    bill = _push(fake)
    result = _run(_adapter().post_payment(_pay(bill, "100.00")))
    assert result.success, result.message
    (txn,) = fake.STATE["netsuite_applied"].values()
    assert txn["apply"] == [{"doc": bill, "amount": Decimal("100.00")}]
    assert txn["externalId"] == f"feohledger-payment-{uuid.UUID(int=7)}"
    assert txn["entity"] == {"id": "25"} and txn["account"] == {"id": "121"}
    assert result.bill_balance_after == Decimal(0)
    report = _report(bill)
    assert report.state is ErpBillState.paid
    assert report.paid_only_by_us and report.our_payment_applied and report.paid_amount is None


def test_post_payment_on_retry_finds_its_own_payment_and_writes_nothing(fake):
    bill = _push(fake)
    first = _run(_adapter().post_payment(_pay(bill)))
    again = _run(_adapter().post_payment(_pay(bill)))
    assert again.success and again.erp_document_id == first.erp_document_id
    assert len(fake.STATE["netsuite_applied"]) == 1


def test_a_discounted_payment_leaves_the_rest_open_and_says_so(fake):
    bill = _push(fake)
    result = _run(_adapter().post_payment(_pay(bill, "98.00")))
    assert result.success
    assert result.bill_balance_after == Decimal("2.00")
    assert _report(bill).state is ErpBillState.partially_paid


def test_post_payment_never_posts_more_than_is_open(fake):
    bill = _push(fake)
    _outside(fake, bill, "VendPymt", "30.00")
    result = _run(_adapter().post_payment(_pay(bill, "100.00")))
    assert not result.success and result.refusal_code == "payment_exceeds_balance"
    assert result.not_posted
    assert len(fake.STATE["netsuite_applied"]) == 1  # only the person's


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"pay_from_account_erp_id": None}, "payment_account_not_mapped"),
        ({"currency": "EUR"}, "currency_mismatch"),
        ({"currency": ""}, "currency_mismatch"),
        ({"amount": Decimal(0)}, "payment_exceeds_balance"),
    ],
)
def test_post_payment_refuses_before_writing(fake, kw, code):
    bill = _push(fake)
    result = _run(_adapter().post_payment(_pay(bill, **kw)))
    assert result.refusal_code == code and result.not_posted
    assert not fake.STATE.get("netsuite_applied")


def test_post_payment_to_a_bill_gone_from_netsuite_is_refused(fake):
    result = _run(_adapter().post_payment(_pay("99999")))
    assert result.refusal_code == "bill_not_in_erp" and result.not_posted


def test_void_payment_deletes_ours_once(fake):
    bill = _push(fake)
    _run(_adapter().post_payment(_pay(bill)))
    first = _run(_adapter().void_payment(_pay(bill)))
    assert first.success and first.message == "Bill payment deleted in NetSuite"
    assert not fake.STATE["netsuite_applied"]
    assert _report(bill).state is ErpBillState.open
    again = _run(_adapter().void_payment(_pay(bill)))
    assert again.success and again.message == "No bill payment of ours to void"


def test_void_payment_leaves_a_persons_payment_alone(fake):
    bill = _push(fake)
    _outside(fake, bill, "VendPymt", "20.00")
    result = _run(_adapter().void_payment(_pay(bill)))
    assert result.success and len(fake.STATE["netsuite_applied"]) == 1
