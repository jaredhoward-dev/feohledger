"""QuickBooks Online status sync-back and BillPayment write-back, adapter half
(decisions §295): `bill_report`, `bills_for_payment`, `changed_bills`,
`post_payment`, `void_payment`.

HTTP is mocked with `patch("httpx.AsyncClient")` like
`test_erp_quickbooks_online_adapter.py`, against a small stateful QuickBooks
holding bills and bill payments.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import erp_oauth
from app.services.erp_adapters.base import (
    BILL_NOT_IN_ERP,
    BILL_PAYMENT_VOIDED,
    PAYMENT_ACCOUNT_NOT_MAPPED,
    PAYMENT_EXCEEDS_BALANCE,
    DiscountCreditPayload,
    ErpBillState,
    ErpListingIncomplete,
    PaymentPayload,
)
from app.services.erp_adapters.quickbooks_online import (
    CURRENCY_MISMATCH,
    QuickBooksOnlineAdapter,
)
from app.utils.json_money import dumps_exact_json

REALM = "9130"
CONFIG = {
    "type": "quickbooks_online",
    "integration_method": "direct",
    "oauth": {"provider": "quickbooks_online", "external_tenant_id": REALM},
}
CORR = "6b0f6d4c-8d2e-4a43-9b8e-3d1c2a7e9f10"
PAYMENT_ID = "0d4c1f7a-2b1e-4c55-8f0a-9e3b6a2d7c41"


def _resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.content = dumps_exact_json(body).encode() if body is not None else b""
    return r


def _link_type(pay_type: str) -> str:
    """How real QuickBooks names a BillPayment on the bill it pays: by its pay
    type, never as "BillPayment" (decisions §317 — the double once used the
    entity name, which hid a reader that matched nothing in a real company)."""
    return "BillPaymentCreditCard" if pay_type == "CreditCard" else "BillPaymentCheck"


def _gone() -> MagicMock:
    return _resp(400, {"Fault": {"Error": [{"Message": "Object Not Found", "code": "610"}]}})


class _Qbo:
    def __init__(self):
        self.calls: list[dict] = []
        self.bills: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.replays: dict[str, dict] = {}
        self.cdc: dict = {"CDCResponse": [{"QueryResponse": [{"Bill": []}, {"BillPayment": []}]}]}
        self.post_answer: MagicMock | None = None
        self.next_id = 900
        self.vendor_credits: dict[str, dict] = {}
        self.fail_apply = False
        self.fail_credit_delete = False
        #: Whether a repeated ``requestid`` on a vendorcredit create replays the
        #: first answer. Intuit documents replay for creates generally, not
        #: for vendorcredit or for how long (decisions §577), so the retry
        #: must not depend on it.
        self.replay_credit_create = True

    def bill(self, bill_id="501", total="100.00", balance=None, note=f"FeohLedger {CORR}", **kw):
        self.bills[bill_id] = {
            "Id": bill_id,
            "TotalAmt": Decimal(total),
            "Balance": Decimal(balance if balance is not None else total),
            "PrivateNote": note,
            "VendorRef": {"value": "56"},
            "SyncToken": "0",
            "LinkedTxn": [],
            # QuickBooks states every bill's currency, the home one included.
            "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
            **kw,
        }
        return self.bills[bill_id]

    def pay(self, bill_id, amount, *, note=None, pay_type="Check", txn_date="2026-09-30"):
        pid = str(self.next_id)
        self.next_id += 1
        self.payments[pid] = {
            "Id": pid,
            "TotalAmt": Decimal(amount),
            "PayType": pay_type,
            "TxnDate": txn_date,
            "PrivateNote": note or "",
            "SyncToken": "0",
            "Line": [
                {"Amount": Decimal(amount), "LinkedTxn": [{"TxnId": bill_id, "TxnType": "Bill"}]}
            ],
        }
        bill = self.bills[bill_id]
        bill["Balance"] -= Decimal(amount)
        bill["LinkedTxn"].append({"TxnId": pid, "TxnType": _link_type(pay_type)})
        return pid

    async def request(self, method, url, params=None, content=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": params, "content": content})
        path = url.split(f"/v3/company/{REALM}/", 1)[1]
        if path == "cdc":
            return _resp(200, self.cdc)
        if path.startswith("bill/"):
            bill = self.bills.get(path.split("/", 1)[1])
            return _resp(200, {"Bill": bill}) if bill else _gone()
        if path.startswith("billpayment/"):
            record = self.payments.get(path.split("/", 1)[1])
            return _resp(200, {"BillPayment": record}) if record else _gone()
        if path.startswith("vendorcredit"):
            return self._vendor_credit(method, path, params, content)
        if method == "POST" and path == "billpayment":
            body = json.loads(content, parse_float=Decimal)
            if (params or {}).get("operation") == "delete":
                record = self.payments.pop(body["Id"])
                for line in record["Line"]:
                    for link in line["LinkedTxn"]:
                        if link["TxnType"] == "Bill":
                            bill = self.bills[link["TxnId"]]
                            bill["Balance"] += line["Amount"]
                            bill["LinkedTxn"] = [
                                t for t in bill["LinkedTxn"] if t["TxnId"] != body["Id"]
                            ]
                return _resp(200, {"BillPayment": {"Id": body["Id"], "status": "Deleted"}})
            if any(
                t["TxnType"] == "VendorCredit"
                for line in body.get("Line") or []
                for t in line["LinkedTxn"]
            ):
                return self._apply_credit(body, params)
            if (params or {}).get("include") == "void":
                record = self.payments[body["Id"]]
                bill_id = record["Line"][0]["LinkedTxn"][0]["TxnId"]
                self.bills[bill_id]["Balance"] += record["TotalAmt"]
                record["TotalAmt"] = Decimal("0")
                return _resp(200, {"BillPayment": record})
            if self.post_answer is not None:
                return self.post_answer
            rid = (params or {}).get("requestid")
            if rid in self.replays:
                return _resp(200, self.replays[rid])
            bill_id = body["Line"][0]["LinkedTxn"][0]["TxnId"]
            pid = self.pay(
                bill_id,
                body["TotalAmt"],
                note=body["PrivateNote"],
                pay_type=body.get("PayType", "Check"),
            )
            self.replays[rid] = {"BillPayment": self.payments[pid]}
            return _resp(200, {"BillPayment": self.payments[pid]})
        raise AssertionError(f"unexpected {method} {url}")

    # VendorCredit and its $0 BillPayment application (decisions §575).
    def _vendor_credit(self, method, path, params, content):
        if method == "GET":
            record = self.vendor_credits.get(path.split("/", 1)[1])
            return _resp(200, {"VendorCredit": record}) if record else _gone()
        body = json.loads(content, parse_float=Decimal)
        if (params or {}).get("operation") == "delete":
            if self.fail_credit_delete:
                return _resp(500, {"Fault": {"Error": [{"code": "6000"}]}})
            self.vendor_credits.pop(body["Id"])
            return _resp(200, {"VendorCredit": {"Id": body["Id"], "status": "Deleted"}})
        rid = (params or {}).get("requestid")
        if self.replay_credit_create and rid in self.replays:
            return _resp(200, self.replays[rid])
        vid = f"vc-{len(self.vendor_credits) + 1}"
        self.vendor_credits[vid] = {**body, "Id": vid, "SyncToken": "0"}
        self.replays[rid] = {"VendorCredit": self.vendor_credits[vid]}
        return _resp(200, self.replays[rid])

    def _apply_credit(self, body, params):
        if self.fail_apply:
            return _resp(400, {"Fault": {"Error": [{"code": "6000", "element": "Line"}]}})
        rid = (params or {}).get("requestid")
        if rid in self.replays:
            return _resp(200, self.replays[rid])
        assert body["TotalAmt"] == 0
        pid = str(self.next_id)
        self.next_id += 1
        self.payments[pid] = {**body, "Id": pid, "SyncToken": "0"}
        for line in body["Line"]:
            for link in line["LinkedTxn"]:
                if link["TxnType"] == "Bill":
                    bill = self.bills[link["TxnId"]]
                    bill["Balance"] -= line["Amount"]
                    bill["LinkedTxn"].append({"TxnId": pid, "TxnType": "BillPaymentCheck"})
        self.replays[rid] = {"BillPayment": self.payments[pid]}
        return _resp(200, self.replays[rid])

    def posts(self):
        return [
            c
            for c in self.calls
            if c["method"] == "POST" and (c["params"] or {}).get("include") != "void"
        ]


@pytest.fixture(autouse=True)
def token():
    with patch.object(erp_oauth, "get_access_token", new=AsyncMock(return_value="tok-1")):
        yield


def _run(qbo: _Qbo, factory):
    with patch("httpx.AsyncClient") as cm:
        cm.return_value.__aenter__.return_value.request = qbo.request
        return asyncio.run(factory())


def _adapter() -> QuickBooksOnlineAdapter:
    return QuickBooksOnlineAdapter(CONFIG)


def _payload(**overrides) -> PaymentPayload:
    base = dict(
        payment_id=PAYMENT_ID,
        bill_id="501",
        amount=Decimal("100.00"),
        currency="USD",
        paid_on=date(2026, 10, 2),
        method="ach",
        pay_from_account_erp_id="35",
    )
    base.update(overrides)
    return PaymentPayload(**base)


def test_supports_status_sync():
    assert _adapter().supports_status_sync is True


# ---------------------------------------------------------------------------
# bill_report
# ---------------------------------------------------------------------------


def test_bill_paid_by_a_person_in_quickbooks():
    qbo = _Qbo()
    qbo.bill()
    qbo.pay("501", "100.00", pay_type="CreditCard", txn_date="2026-09-30")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.correlation_id == CORR
    assert report.paid_on == date(2026, 9, 30)
    # A company card is a card rail: 1099-K, not the payer's 1099.
    assert report.payment_method == "card"
    assert report.external_payment_ids and not report.paid_only_by_us


@pytest.mark.parametrize(
    ("pay_type", "link_type", "method"),
    [
        ("Check", "BillPaymentCheck", "check"),
        ("CreditCard", "BillPaymentCreditCard", "card"),
    ],
)
def test_a_cash_payment_made_in_quickbooks_is_cash_not_a_vendor_credit(pay_type, link_type, method):
    """Regression (production, 2026-10-22): a person paid a $150 bill in
    QuickBooks with Pay Bills from Checking. The bill named the payment as
    `BillPaymentCheck`, the reader looked for `BillPayment`, found no payment,
    and reported the bill settled by a vendor credit — which opened a blocking
    exception instead of recording the invoice paid outside FeohLedger."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    pid = qbo.pay("501", "150.00", pay_type=pay_type, txn_date="2026-10-21")
    assert qbo.bills["501"]["LinkedTxn"] == [{"TxnId": pid, "TxnType": link_type}]
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.reduced_outside is True
    assert report.settled_by_credit is False
    assert report.paid_on == date(2026, 10, 21)
    assert report.payment_method == method
    assert report.external_payment_ids == (pid,)
    assert report.paid_only_by_us is False


# -- the cash QuickBooks shows (decisions §393) ------------------------------


def test_a_bill_paid_in_full_in_cash_states_the_cash_and_its_currency():
    """The status sync records QuickBooks' own figure, never FeohLedger's."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "150.00", pay_type="Check", txn_date="2026-10-21")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.paid_amount == Decimal("150.00")
    assert report.paid_currency == "USD"
    assert report.payment_method == "check"


def test_cash_is_what_the_lines_applied_to_this_bill_not_the_payments_total():
    """One BillPayment can pay several bills; only its lines on THIS bill are
    this bill's cash."""
    qbo = _Qbo()
    qbo.bill("501", total="150.00")
    qbo.bill("502", total="70.00")
    _payment_record(
        qbo, "501", "220.00", [_line("Bill", "501", "150.00"), _line("Bill", "502", "70.00")]
    )
    # The helper takes every Bill line off the bill it names; QuickBooks takes
    # each line off its own bill.
    qbo.bills["501"]["Balance"] = Decimal("0")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid and not report.settled_by_credit
    assert report.paid_amount == Decimal("150.00")


def test_a_bill_paid_part_by_cheque_part_by_card_states_the_sum_and_no_one_rail():
    """Both are cash, so the bill is paid in cash (§303) at their sum. Neither
    rail is the whole payment's, so none is stated (1099-reportable), rather
    than the later payment's standing for both."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "100.00", pay_type="Check", txn_date="2026-10-20")
    qbo.pay("501", "50.00", pay_type="CreditCard", txn_date="2026-10-21")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid and not report.settled_by_credit
    assert report.paid_amount == Decimal("150.00")
    assert report.payment_method is None
    assert report.paid_on == date(2026, 10, 21)


def test_a_cheque_and_card_bill_states_each_payments_rail_and_amount():
    """The split the 1099 needs (decisions §396): each cash payment's rail and
    what it applied to THIS bill, summing exactly to the cash figure. No
    account, no payee: a rail and an amount per payment."""
    from app.services.payment_methods import RailPart

    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "100.00", pay_type="Check", txn_date="2026-10-20")
    qbo.pay("501", "50.00", pay_type="CreditCard", txn_date="2026-10-21")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.payment_parts == (
        RailPart("check", Decimal("100.00")),
        RailPart("card", Decimal("50.00")),
    )
    assert sum(p.amount for p in report.payment_parts) == report.paid_amount


def test_the_breakdown_counts_only_the_lines_on_this_bill():
    from app.services.payment_methods import RailPart

    qbo = _Qbo()
    qbo.bill("501", total="150.00")
    qbo.bill("502", total="70.00")
    _payment_record(
        qbo, "501", "220.00", [_line("Bill", "501", "150.00"), _line("Bill", "502", "70.00")]
    )
    qbo.bills["501"]["Balance"] = Decimal("0")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.payment_parts == (RailPart("check", Decimal("150.00")),)


def test_no_breakdown_is_stated_without_a_cash_figure():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "50.00", pay_type="CreditCard")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.paid_amount is None and report.payment_parts == ()


def test_two_cheques_keep_their_rail():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "100.00", pay_type="Check", txn_date="2026-10-20")
    qbo.pay("501", "50.00", pay_type="Check", txn_date="2026-10-21")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.paid_amount == Decimal("150.00") and report.payment_method == "check"


def test_a_part_paid_bill_states_no_cash_amount():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "50.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.partially_paid
    assert report.paid_amount is None


@pytest.mark.parametrize("pay_type", ["Check", "CreditCard"])
def test_a_bill_paid_part_by_a_vendor_credit_states_no_cash_amount(pay_type):
    """§303: a vendor credit is not money leaving. Settled by credit, so the
    cash beside it is never booked as the payment of the whole bill."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "100.00", pay_type=pay_type)
    _apply_credit(qbo, "501", "50.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.settled_by_credit is True
    assert report.paid_amount is None


def test_a_bill_we_part_paid_ourselves_states_no_cash_amount():
    """Our write-back and a person's cheque together: no one payment a record
    could state, as Xero answers (§386)."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "100.00", note=f"FeohLedger payment {PAYMENT_ID}")
    qbo.pay("501", "50.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid and report.reduced_outside
    assert report.paid_amount is None
    # Stated, so the sync blocks such a bill rather than book the owed figure.
    assert report.our_payment_applied is True


def test_a_voided_write_back_of_ours_is_not_applied():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    pid = qbo.pay("501", "150.00", note=f"FeohLedger payment {PAYMENT_ID}")
    qbo.payments[pid]["TotalAmt"] = Decimal("0")
    qbo.bills["501"]["Balance"] = Decimal("150.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.our_payment_applied is False


def test_a_bill_paid_only_by_our_write_back_states_no_cash_amount():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "150.00", note=f"FeohLedger payment {PAYMENT_ID}")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.paid_only_by_us and report.paid_amount is None


def test_the_cash_is_in_the_bills_currency():
    qbo = _Qbo()
    qbo.bill(total="150.00", CurrencyRef={"value": "cad", "name": "Canadian Dollar"})
    qbo.pay("501", "150.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.paid_amount == Decimal("150.00") and report.paid_currency == "CAD"


def test_a_bill_stating_no_currency_states_none_for_its_cash():
    """The sync refuses an amount in no currency (§386): never a bare number."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    del qbo.bills["501"]["CurrencyRef"]
    qbo.pay("501", "150.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.paid_amount == Decimal("150.00") and report.paid_currency is None


def test_a_payment_stating_another_currency_than_its_bill_states_none():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    pid = qbo.pay("501", "150.00")
    qbo.payments[pid]["CurrencyRef"] = {"value": "EUR", "name": "Euro"}
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    # The amount stays stated: the sync judges a currency only beside one, so
    # dropping it here would book the owed figure instead of refusing.
    assert report.paid_amount == Decimal("150.00")
    assert report.paid_currency is None


def _payment_record(qbo: _Qbo, bill_id: str, total: str, lines: list[dict], note="") -> str:
    """A BillPayment a person made in QuickBooks, with the given lines, linked
    from the bill as real QuickBooks does; the bill comes down by what the
    lines applied to it."""
    pid = str(qbo.next_id)
    qbo.next_id += 1
    qbo.payments[pid] = {
        "Id": pid,
        "TotalAmt": Decimal(total),
        "PayType": "Check",
        "TxnDate": "2026-10-21",
        "PrivateNote": note,
        "SyncToken": "0",
        "Line": lines,
    }
    bill = qbo.bills[bill_id]
    for line in lines:
        if line["LinkedTxn"][0]["TxnType"] == "Bill":
            bill["Balance"] -= line["Amount"]
    bill["LinkedTxn"].append({"TxnId": pid, "TxnType": _link_type("Check")})
    return pid


def _line(txn_type: str, txn_id: str, amount: str) -> dict:
    return {"Amount": Decimal(amount), "LinkedTxn": [{"TxnId": txn_id, "TxnType": txn_type}]}


def test_a_journal_entry_credit_cleared_in_pay_bills_is_not_cash():
    """Cash is defined positively. A $0 BillPayment applying a journal-entry
    credit is not a vendor credit, and must still not be booked as a cheque
    (money-path review of §317: reading payments correctly made it reachable)."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    _payment_record(
        qbo, "501", "0", [_line("Bill", "501", "150.00"), _line("JournalEntry", "JE-1", "150.00")]
    )
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and report.settled_by_credit
    assert report.paid_on is None and report.payment_method is None


def test_a_payment_part_cash_part_journal_entry_credit_still_blocks():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    _payment_record(
        qbo,
        "501",
        "50.00",
        [_line("Bill", "501", "150.00"), _line("JournalEntry", "JE-1", "100.00")],
    )
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.settled_by_credit is True


def test_a_zero_total_payment_without_a_void_note_is_not_cash():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    _payment_record(qbo, "501", "0", [_line("Bill", "501", "150.00")], note="tidied up")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.settled_by_credit is True
    assert report.paid_on is None and report.payment_method is None


def test_cash_that_does_not_cover_the_reduction_still_blocks():
    """$100 of cash read, $150 gone from the bill outside us (a journal entry
    against the bill itself, say): the uncovered $50 left no money trail."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "100.00")
    qbo.bills["501"]["Balance"] = Decimal("0")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.settled_by_credit is True


def test_a_bill_payment_linked_by_its_entity_name_is_not_read():
    """`BillPayment` is the entity, not a `LinkedTxn.TxnType` a Bill carries.
    It is deliberately not accepted (decisions §317): with no payment read, a
    bill taken down outside us still blocks for a person, never books cash."""
    qbo = _Qbo()
    qbo.bill()
    pid = qbo.pay("501", "100.00")
    qbo.bills["501"]["LinkedTxn"] = [{"TxnId": pid, "TxnType": "BillPayment"}]
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.external_payment_ids == ()
    assert report.reduced_outside and report.settled_by_credit
    assert not any(c["url"].endswith(f"billpayment/{pid}") for c in qbo.calls)


def test_our_write_back_is_found_through_the_bills_real_link_and_voided():
    """The write-back's idempotency and void both find our BillPayment through
    the bill. Read with the wrong link type, a void answered "nothing of ours
    to void" and left our payment standing in QuickBooks."""
    qbo = _Qbo()
    qbo.bill()
    posted = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert posted.success
    assert qbo.bills["501"]["LinkedTxn"][0]["TxnType"] == "BillPaymentCheck"
    voided = _run(qbo, lambda: _adapter().void_payment(_payload()))
    assert voided.success and voided.erp_document_id == posted.erp_document_id
    assert qbo.payments[posted.erp_document_id]["TotalAmt"] == Decimal("0")


def test_bill_paid_only_by_our_write_back_is_not_news():
    qbo = _Qbo()
    qbo.bill()
    qbo.pay("501", "100.00", note=f"FeohLedger payment {PAYMENT_ID}")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.paid_only_by_us is True
    assert report.external_payment_ids == ()


def _apply_credit(qbo: _Qbo, bill_id: str, amount: str) -> str:
    """What QuickBooks records when a vendor credit is applied to a bill: a $0
    BillPayment with one line on the bill and one on the VendorCredit."""
    pid = str(qbo.next_id)
    qbo.next_id += 1
    qbo.payments[pid] = {
        "Id": pid,
        "TotalAmt": Decimal("0"),
        "PayType": "Check",
        "TxnDate": "2026-09-30",
        "PrivateNote": "",
        "SyncToken": "0",
        "Line": [
            {"Amount": Decimal(amount), "LinkedTxn": [{"TxnId": bill_id, "TxnType": "Bill"}]},
            {
                "Amount": Decimal(amount),
                "LinkedTxn": [{"TxnId": "VC-1", "TxnType": "VendorCredit"}],
            },
        ],
    }
    bill = qbo.bills[bill_id]
    bill["Balance"] -= Decimal(amount)
    bill["LinkedTxn"].append({"TxnId": pid, "TxnType": _link_type("Check")})
    return pid


def test_a_bill_settled_by_a_vendor_credit_is_not_our_write_back_nor_cash():
    """A $0 BillPayment is a credit, not a void: the bill was settled outside
    FeohLedger, without money leaving (money-path review, §295)."""
    qbo = _Qbo()
    qbo.bill()
    _apply_credit(qbo, "501", "100.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and report.settled_by_credit
    assert report.paid_only_by_us is False
    assert report.paid_on is None and report.payment_method is None


def test_a_partial_credit_beside_our_write_back_is_still_outside_us():
    qbo = _Qbo()
    qbo.bill(total="100.00")
    _apply_credit(qbo, "501", "20.00")
    _run(qbo, lambda: _adapter().post_payment(_payload(amount=Decimal("80.00"))))
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and report.settled_by_credit
    assert report.paid_only_by_us is False


def test_a_person_voided_in_quickbooks_leaves_nothing_outside_us():
    qbo = _Qbo()
    qbo.bill()
    pid = qbo.pay("501", "100.00")
    qbo.payments[pid].update(TotalAmt=Decimal("0"), PrivateNote="Voided")
    qbo.bills["501"]["Balance"] = Decimal("100.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.open and report.reduced_outside is False


def test_partial_open_deleted_and_unknown():
    qbo = _Qbo()
    qbo.bill("501", total="100.00")
    qbo.pay("501", "40.00")
    qbo.bill("502", total="50.00")
    qbo.bill("503", total="0.00")

    async def read_all():
        return [await _adapter().bill_report(b) for b in ("501", "502", "503", "999")]

    reports = _run(qbo, read_all)
    assert [r.state for r in reports] == [
        ErpBillState.partially_paid,
        ErpBillState.open,
        ErpBillState.unknown,
        ErpBillState.deleted,  # Fault 610 — QuickBooks' positive "gone"
    ]


def test_a_bill_we_never_marked_names_no_invoice():
    qbo = _Qbo()
    qbo.bill(note="typed in QuickBooks by hand")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.correlation_id is None


def test_an_unreadable_bill_raises_rather_than_reads_as_deleted():
    qbo = _Qbo()

    async def broken(method, url, **kw):
        return _resp(503)

    qbo.request = broken
    with pytest.raises(Exception):  # noqa: B017 — any failure, never a report
        _run(qbo, lambda: _adapter().bill_report("501"))


# ---------------------------------------------------------------------------
# bills_for_payment / changed_bills
# ---------------------------------------------------------------------------


def test_bills_for_payment_reads_the_linked_bills():
    qbo = _Qbo()
    qbo.bill()
    pid = qbo.pay("501", "10.00")
    assert _run(qbo, lambda: _adapter().bills_for_payment(pid)) == ["501"]
    assert _run(qbo, lambda: _adapter().bills_for_payment("404")) == []


def test_changed_bills_collects_bills_and_payment_links():
    qbo = _Qbo()
    qbo.cdc = {
        "CDCResponse": [
            {
                "QueryResponse": [
                    {"Bill": [{"Id": "501"}, {"Id": "502", "status": "Deleted"}]},
                    {
                        "BillPayment": [
                            {
                                "Id": "9",
                                "Line": [{"LinkedTxn": [{"TxnId": "503", "TxnType": "Bill"}]}],
                            },
                            {"Id": "10", "status": "Deleted"},
                        ]
                    },
                ]
            }
        ]
    }
    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert _run(qbo, lambda: _adapter().changed_bills(since)) == ["501", "502", "503"]
    (call,) = qbo.calls
    assert call["params"]["entities"] == "Bill,BillPayment"
    assert call["params"]["changedSince"] == "2026-10-01T00:00:00+00:00"


def test_a_full_cdc_page_raises_instead_of_returning_part():
    qbo = _Qbo()
    qbo.cdc = {
        "CDCResponse": [{"QueryResponse": [{"Bill": [{"Id": str(i)} for i in range(1000)]}]}]
    }
    with pytest.raises(ErpListingIncomplete):
        _run(qbo, lambda: _adapter().changed_bills(datetime(2026, 10, 1, tzinfo=UTC)))


# ---------------------------------------------------------------------------
# post_payment
# ---------------------------------------------------------------------------


def test_post_payment_writes_one_bill_payment_against_the_bill():
    qbo = _Qbo()
    qbo.bill()
    result = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert result.success, result.message
    (post,) = qbo.posts()
    assert post["params"]["requestid"] == PAYMENT_ID
    body = json.loads(post["content"], parse_float=Decimal)
    assert body["PayType"] == "Check"
    assert body["CheckPayment"] == {"BankAccountRef": {"value": "35"}}
    assert body["Line"][0]["LinkedTxn"] == [{"TxnId": "501", "TxnType": "Bill"}]
    assert body["TotalAmt"] == Decimal("100.00")
    # Money on the wire is exact: a JSON number, never a float rendering.
    assert '"TotalAmt": 100.00' in post["content"] or '"TotalAmt":100.00' in post["content"]
    assert body["PrivateNote"] == f"FeohLedger payment {PAYMENT_ID}"
    assert result.bill_balance_after == Decimal("0")


def test_post_payment_is_idempotent_on_the_payment_id():
    qbo = _Qbo()
    qbo.bill()
    first = _run(qbo, lambda: _adapter().post_payment(_payload()))
    again = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert first.success and again.success
    assert again.erp_document_id == first.erp_document_id
    assert len(qbo.posts()) == 1  # the retry found our own earlier write


def test_a_re_sync_after_a_further_card_clearing_posts_no_second_bill_payment():
    """Decisions §562: a further card clearing raises the leg's settled figure
    and re-dispatches the payment sync, whose write-back reaches the adapter
    again for a payment already in QuickBooks — now with a different amount.
    Our earlier BillPayment is found by its PrivateNote marker and returned; no
    second one is posted. The difference is the over-settlement flag's to
    reconcile, not a second write."""
    qbo = _Qbo()
    qbo.bill(total="150.00")
    first = _run(qbo, lambda: _adapter().post_payment(_payload()))
    again = _run(qbo, lambda: _adapter().post_payment(_payload(amount=Decimal("150.00"))))
    assert first.success and again.success
    assert again.erp_document_id == first.erp_document_id
    assert len(qbo.posts()) == 1
    assert qbo.bills["501"]["Balance"] == Decimal("50.00")  # only the first write landed


def test_a_card_payment_books_from_the_card_account():
    qbo = _Qbo()
    qbo.bill()
    result = _run(qbo, lambda: _adapter().post_payment(_payload(card=True, method="virtual_card")))
    assert result.success
    body = json.loads(qbo.posts()[0]["content"])
    assert body["PayType"] == "CreditCard"
    assert body["CreditCardPayment"] == {"CCAccountRef": {"value": "35"}}
    # Linked on the bill as a card payment, and still found as ours.
    assert qbo.bills["501"]["LinkedTxn"][0]["TxnType"] == "BillPaymentCreditCard"
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.paid_only_by_us is True


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"pay_from_account_erp_id": None}, PAYMENT_ACCOUNT_NOT_MAPPED),
        ({"bill_id": "999"}, BILL_NOT_IN_ERP),
        ({"amount": Decimal("100.01")}, PAYMENT_EXCEEDS_BALANCE),
        ({"currency": "EUR"}, "currency_mismatch"),
        # A payment stating no currency is not known to be in the bill's.
        ({"currency": ""}, "currency_mismatch"),
    ],
)
def test_post_payment_refuses_before_writing(overrides, code):
    qbo = _Qbo()
    qbo.bill(CurrencyRef={"value": "USD"})
    result = _run(qbo, lambda: _adapter().post_payment(_payload(**overrides)))
    assert not result.success and not result.retryable
    assert result.refusal_code == code
    assert result.not_posted is True
    assert qbo.posts() == []


def test_a_bill_stating_no_currency_is_refused_not_paid_as_a_bare_number():
    qbo = _Qbo()
    qbo.bill(CurrencyRef=None)
    result = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert not result.success and result.refusal_code == "currency_mismatch"
    assert result.not_posted is True
    assert qbo.posts() == []


def test_the_write_back_names_the_bills_currency():
    qbo = _Qbo()
    qbo.bill()
    assert _run(qbo, lambda: _adapter().post_payment(_payload())).success
    assert json.loads(qbo.posts()[0]["content"])["CurrencyRef"] == {"value": "USD"}


def test_a_replay_of_a_payment_since_voided_is_refused_not_reported_written():
    qbo = _Qbo()
    qbo.bill()
    first = _run(qbo, lambda: _adapter().post_payment(_payload()))
    _run(qbo, lambda: _adapter().void_payment(_payload()))  # voided "in QuickBooks"
    again = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert first.success
    assert not again.success and again.refusal_code == BILL_PAYMENT_VOIDED
    assert len(qbo.posts()) == 1


def test_a_refused_bill_payment_is_coded_from_the_fault_number_only():
    qbo = _Qbo()
    qbo.bill()
    qbo.post_answer = _resp(
        400,
        {
            "Fault": {
                "Error": [
                    {
                        "Message": "Account Period Closed",
                        "Detail": "Vendor Acme Supplies paid 100.00 from 1234-5678",
                        "code": "6210",
                    }
                ]
            }
        },
    )
    result = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert result.refusal_code == "period_closed"
    assert result.refusal_params == {"intuit_code": "6210"}
    assert "Acme" not in (result.message or "") and "1234" not in (result.message or "")


def test_a_throttled_write_back_stays_retryable():
    qbo = _Qbo()
    qbo.bill()
    qbo.post_answer = _resp(429)
    result = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert not result.success and result.retryable


# ---------------------------------------------------------------------------
# void_payment
# ---------------------------------------------------------------------------


def test_void_payment_voids_only_our_own_bill_payment():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "50.00", note="paid by hand")
    _run(qbo, lambda: _adapter().post_payment(_payload()))
    result = _run(qbo, lambda: _adapter().void_payment(_payload()))
    assert result.success
    voids = [c for c in qbo.calls if (c["params"] or {}).get("include") == "void"]
    assert len(voids) == 1
    body = json.loads(voids[0]["content"])
    assert body["sparse"] is True and body["SyncToken"] == "0"
    assert qbo.bills["501"]["Balance"] == Decimal("100.00")
    # Nothing of ours left: a second void is a no-op success.
    assert _run(qbo, lambda: _adapter().void_payment(_payload())).success
    assert len([c for c in qbo.calls if (c["params"] or {}).get("include") == "void"]) == 1


def test_an_erp_without_status_sync_answers_fail_closed_by_default():
    """The base defaults (decisions §295): an ERP that does not report back is
    not a skeleton, but nothing it answers may read as "paid" or "open"."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    # NetSuite reported back from §536, so another direct ERP stands in.
    adapter = get_erp_adapter({"type": "dynamics_365_bc", "integration_method": "direct"})
    assert adapter.supports_status_sync is False
    report = asyncio.run(adapter.bill_report("1"))
    assert report.state is ErpBillState.unknown
    written = asyncio.run(adapter.post_payment(_payload()))
    assert written.success is False and written.retryable is False
    assert asyncio.run(adapter.void_payment(_payload())).success is False
    assert asyncio.run(adapter.bills_for_payment("9")) == []
    assert asyncio.run(adapter.changed_bills(datetime(2026, 10, 1, tzinfo=UTC))) == []


# ---------------------------------------------------------------------------
# early-payment discount booked as a VendorCredit (decisions §575)


def _credit(**overrides) -> DiscountCreditPayload:
    base = dict(
        payment_id=PAYMENT_ID,
        bill_id="501",
        amount=Decimal("2.00"),
        currency="USD",
        credit_on=date(2026, 10, 2),
        discount_account_erp_id="88",
        pay_from_account_erp_id="35",
    )
    base.update(overrides)
    return DiscountCreditPayload(**base)


def _discounted(qbo: _Qbo) -> dict:
    bill = qbo.bill()
    assert _run(qbo, lambda: _adapter().post_payment(_payload(amount=Decimal("98.00")))).success
    assert bill["Balance"] == Decimal("2.00")
    return bill


def _bodies(qbo: _Qbo, path: str) -> list[dict]:
    return [
        json.loads(c["content"], parse_float=Decimal)
        for c in qbo.calls
        if c["method"] == "POST"
        and c["url"].endswith(f"/{path}")
        and not (c["params"] or {}).get("operation")
    ]


def test_the_discount_is_booked_as_a_vendor_credit_applied_to_the_bill():
    qbo = _Qbo()
    bill = _discounted(qbo)
    result = _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    assert result.success, result.message
    assert result.erp_document_id == "vc-1" and result.bill_balance_after == Decimal("0")
    [credit] = _bodies(qbo, "vendorcredit")
    assert credit["VendorRef"] == {"value": "56"}
    assert credit["PrivateNote"] == f"FeohLedger discount {PAYMENT_ID}"
    assert credit["CurrencyRef"] == {"value": "USD"}
    assert credit["Line"] == [
        {
            "Amount": Decimal("2.00"),
            "DetailType": "AccountBasedExpenseLineDetail",
            "Description": "Early-payment discount",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "88"}},
        }
    ]
    apply = _bodies(qbo, "billpayment")[-1]
    assert apply["TotalAmt"] == 0
    assert [line["LinkedTxn"] for line in apply["Line"]] == [
        [{"TxnId": "501", "TxnType": "Bill"}],
        [{"TxnId": "vc-1", "TxnType": "VendorCredit"}],
    ]
    # Our payment plus our credit are ours: not news to the status sync, and
    # the $0 application is never mistaken for a voided write-back of ours.
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.paid_only_by_us and not report.reduced_outside
    assert bill["Balance"] == 0
    replay = _run(qbo, lambda: _adapter().post_payment(_payload(amount=Decimal("98.00"))))
    assert replay.success


def test_a_replay_of_the_discount_credit_books_nothing_twice():
    qbo = _Qbo()
    _discounted(qbo)
    first = _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    again = _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    assert again.success and again.erp_document_id == first.erp_document_id
    assert len(_bodies(qbo, "vendorcredit")) == 1 and len(qbo.vendor_credits) == 1


def test_a_credit_created_but_not_applied_is_applied_once_on_retry():
    """The failed apply names the credit; the write-back records it and the
    retry applies THAT credit by id. Intuit's ``requestid`` replay is not
    relied on: the double here creates a fresh credit for a repeated key
    (decisions §577)."""
    qbo = _Qbo()
    qbo.replay_credit_create = False
    bill = _discounted(qbo)
    qbo.fail_apply = True
    failed = _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    assert not failed.success and not failed.not_posted
    assert failed.erp_document_id == "vc-1"
    qbo.fail_apply = False
    retried = _run(
        qbo, lambda: _adapter().post_discount_credit(_credit(credit_id=failed.erp_document_id))
    )
    assert retried.success and retried.erp_document_id == "vc-1"
    assert len(qbo.vendor_credits) == 1 and len(_bodies(qbo, "vendorcredit")) == 1
    assert bill["Balance"] == 0


def test_an_unapplied_credit_is_not_found_through_the_bill():
    """Why a recorded credit with no known id goes to a person (decisions
    §579): ``find_discount_credit`` reads the bill's applying BillPayments, so
    a VendorCredit whose create answer was lost and that nothing applied is
    never found there — a miss is not proof none stands, and the write-back
    holds the record rather than create again."""
    qbo = _Qbo()
    _discounted(qbo)
    qbo.fail_apply = True
    failed = _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    assert failed.erp_document_id == "vc-1" and "vc-1" in qbo.vendor_credits
    assert _run(qbo, lambda: _adapter().find_discount_credit(_credit())) is None


def test_a_recorded_credit_someone_deleted_is_not_booked_again():
    qbo = _Qbo()
    _discounted(qbo)
    qbo.fail_apply = True
    failed = _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    qbo.fail_apply = False
    qbo.vendor_credits.clear()
    retried = _run(qbo, lambda: _adapter().post_discount_credit(_credit(credit_id="vc-1")))
    assert not retried.success and retried.refusal_code == BILL_PAYMENT_VOIDED
    assert retried.refusal_params == {"document": "discount_credit"}
    assert failed.erp_document_id == "vc-1" and len(_bodies(qbo, "vendorcredit")) == 1


def test_a_void_that_deleted_the_application_but_not_the_credit_finishes_by_id():
    qbo = _Qbo()
    bill = _discounted(qbo)
    _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    credit_id = _run(qbo, lambda: _adapter().find_discount_credit(_credit()))
    assert credit_id == "vc-1"
    qbo.fail_credit_delete = True
    first = _run(qbo, lambda: _adapter().void_discount_credit(_credit(credit_id=credit_id)))
    assert not first.success and first.erp_document_id == "vc-1"
    # The application is gone, so the bill no longer leads to the credit…
    assert _run(qbo, lambda: _adapter().find_discount_credit(_credit())) is None
    assert "vc-1" in qbo.vendor_credits and bill["Balance"] == Decimal("2.00")
    qbo.fail_credit_delete = False
    # …and without the recorded id the retry would report success over it.
    retried = _run(qbo, lambda: _adapter().void_discount_credit(_credit(credit_id=credit_id)))
    assert retried.success and retried.erp_document_id == "vc-1"
    assert qbo.vendor_credits == {}


def test_an_unapplied_credit_is_deleted_by_its_recorded_id_on_void():
    qbo = _Qbo()
    _discounted(qbo)
    qbo.fail_apply = True
    failed = _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    voided = _run(
        qbo, lambda: _adapter().void_discount_credit(_credit(credit_id=failed.erp_document_id))
    )
    assert voided.success and qbo.vendor_credits == {}


def test_voiding_deletes_the_application_and_the_credit_once():
    qbo = _Qbo()
    bill = _discounted(qbo)
    _run(qbo, lambda: _adapter().post_discount_credit(_credit()))
    voided = _run(qbo, lambda: _adapter().void_discount_credit(_credit()))
    assert voided.success and voided.erp_document_id == "vc-1"
    assert qbo.vendor_credits == {} and bill["Balance"] == Decimal("2.00")
    again = _run(qbo, lambda: _adapter().void_discount_credit(_credit()))
    assert again.success and again.erp_document_id is None


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"currency": "EUR"}, CURRENCY_MISMATCH),
        ({"amount": Decimal("2.01")}, PAYMENT_EXCEEDS_BALANCE),
        ({"discount_account_erp_id": None}, PAYMENT_ACCOUNT_NOT_MAPPED),
    ],
)
def test_the_discount_credit_refuses_before_writing(overrides, code):
    qbo = _Qbo()
    _discounted(qbo)
    result = _run(qbo, lambda: _adapter().post_discount_credit(_credit(**overrides)))
    assert not result.success and result.refusal_code == code and result.not_posted
    assert not _bodies(qbo, "vendorcredit")
