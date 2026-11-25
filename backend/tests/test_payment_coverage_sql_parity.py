"""`payment_covered_sql` is `payment_coverage(...).state == "covered"`, in SQL.

`open_ap_due.paid_out` (the overdue counts, `?overdue=true`, the /cfo card and
the forecast) needs the coverage verdict set-based, so it reads the SQL twin;
the payment sync and an ERP `paid` report read the Python one. Every case
below is run through both against a real tenant, so the two cannot drift
(decisions §569).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.virtual_card import VirtualCard
from app.services.payment_settlement_record import payment_coverage, payment_covered_sql

pytestmark = pytest.mark.asyncio

TENANT = "a"
NOW = datetime(2026, 11, 20, tzinfo=UTC)

#: name -> (invoice currency, payment kwargs, [card kwargs, oldest first])
CASES: dict[str, tuple] = {
    "rail_reported_nothing": ("USD", {}, []),
    "rail_settled_in_full": ("USD", {"settled_amount": "10000.00", "settled_currency": "USD"}, []),
    "rail_settled_short": ("USD", {"settled_amount": "9500.00", "settled_currency": "USD"}, []),
    "rail_within_tolerance": ("USD", {"settled_amount": "9999.99", "settled_currency": "USD"}, []),
    "rail_a_cent_past_tolerance": (
        "USD",
        {"settled_amount": "9999.98", "settled_currency": "USD"},
        [],
    ),
    "rail_over_settled": ("USD", {"settled_amount": "12000.00", "settled_currency": "USD"}, []),
    "rail_unstorable": ("USD", {"settled_amount_unstorable": True}, []),
    "rail_wrong_currency": ("USD", {"settled_amount": "10000.00", "settled_currency": "EUR"}, []),
    "rail_case_and_space_in_code": (
        "USD",
        {"settled_amount": "10000.00", "settled_currency": "usd"},
        [],
    ),
    "rail_no_currency_reported": ("USD", {"settled_amount": "10000.00"}, []),
    "invoice_currency_unknown": (
        None,
        {"settled_amount": "10000.00", "settled_currency": "USD"},
        [],
    ),
    "fx_settled_on_the_source_leg": (
        "EUR",
        {
            "source_amount": "10900.00",
            "source_currency": "USD",
            "settled_amount": "10900.00",
            "settled_currency": "USD",
        },
        [],
    ),
    "fx_short_on_the_source_leg": (
        "EUR",
        {
            "source_amount": "10900.00",
            "source_currency": "USD",
            "settled_amount": "10000.00",
            "settled_currency": "USD",
        },
        [],
    ),
    "card_none_found": ("USD", {"method": "virtual_card"}, []),
    "card_minted_uncharged": ("USD", {"method": "virtual_card"}, [{"status": "active"}]),
    "card_authorized_only": (
        "USD",
        {"method": "virtual_card"},
        [{"status": "charged", "amount_charged": "10000.00"}],
    ),
    "card_authorized_with_stale_stamp": (
        "USD",
        {"method": "virtual_card", "settled_amount": "10000.00", "settled_currency": "USD"},
        [{"status": "charged", "amount_charged": "10000.00"}],
    ),
    "card_settled_in_full_unstamped": (
        "USD",
        {"method": "virtual_card"},
        [{"status": "completed", "amount_charged": "10000.00"}],
    ),
    "card_settled_short_unstamped": (
        "USD",
        {"method": "virtual_card"},
        [{"status": "completed", "amount_charged": "9500.00"}],
    ),
    "card_settled_stamped_in_full": (
        "USD",
        {"method": "virtual_card", "settled_amount": "10000.00", "settled_currency": "USD"},
        [{"status": "completed", "amount_charged": "10000.00"}],
    ),
    "card_settled_no_figure": ("USD", {"method": "virtual_card"}, [{"status": "completed"}]),
    "card_settled_other_currency": (
        "USD",
        {"method": "virtual_card"},
        [{"status": "completed", "amount_charged": "10000.00", "currency": "EUR"}],
    ),
    # One live card per invoice (`uq_virtual_cards_one_live_per_invoice`), so a
    # second card is a cancelled one; the settled card is picked first.
    "card_settled_then_newer_cancelled": (
        "USD",
        {"method": "virtual_card"},
        [
            {"status": "completed", "amount_charged": "10000.00"},
            {"status": "cancelled"},
        ],
    ),
    "card_cancelled_then_newer_unspent": (
        "USD",
        {"method": "virtual_card"},
        [
            {"status": "cancelled"},
            {"status": "active"},
        ],
    ),
    "card_unlinked_settled_on_the_invoice": (
        "USD",
        {"method": "virtual_card"},
        [{"status": "completed", "amount_charged": "10000.00", "_unlinked": True}],
    ),
}

#: The verdict each case must reach, so the parity cannot pass by both sides
#: agreeing on something wrong.
EXPECTED_COVERED = {
    "rail_reported_nothing",
    "rail_settled_in_full",
    "rail_within_tolerance",
    "rail_over_settled",
    "rail_case_and_space_in_code",
    "rail_no_currency_reported",
    "invoice_currency_unknown",
    "fx_settled_on_the_source_leg",
    "card_settled_in_full_unstamped",
    "card_settled_stamped_in_full",
    "card_settled_no_figure",
    "card_settled_then_newer_cancelled",
    "card_unlinked_settled_on_the_invoice",
}


def _dec(kw: dict) -> dict:
    return {
        k: Decimal(v) if k in {"settled_amount", "source_amount", "amount_charged"} else v
        for k, v in kw.items()
    }


async def test_sql_and_python_coverage_agree_on_every_case(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    ids: dict[str, uuid.UUID] = {}
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        for name, (inv_ccy, pay_kw, cards) in CASES.items():
            inv = Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=f"COV-{name}"[:50],
                vendor_name="Coverage Parity",
                amount=Decimal("10000.00"),
                currency=inv_ccy,
                status=InvoiceStatus.payment_scheduled,
            )
            s.add(inv)
            await s.flush()
            pay = Payment(
                entity_id=ent,
                invoice_id=inv.id,
                amount=Decimal("10000.00"),
                provider="mock",
                status="completed",
                completed_at=NOW,
                **{"method": "ach", **_dec(pay_kw)},
            )
            s.add(pay)
            await s.flush()
            for n, card_kw in enumerate(cards):
                card_kw = dict(card_kw)
                unlinked = card_kw.pop("_unlinked", False)
                s.add(
                    VirtualCard(
                        organization_id=org_id,
                        entity_id=ent,
                        invoice_id=inv.id,
                        payment_id=None if unlinked else pay.id,
                        card_provider="mock",
                        provider_card_id=f"pc-{uuid.uuid4().hex[:12]}",
                        amount_limit=Decimal("10000.00"),
                        created_at=NOW + timedelta(minutes=n),
                        **_dec(card_kw),
                    )
                )
            ids[name] = pay.id
        await s.commit()

    async with mk() as s:
        sql_verdict = dict(
            (
                await s.execute(
                    select(Payment.id, payment_covered_sql())
                    .join(Invoice, Invoice.id == Payment.invoice_id)
                    .where(Payment.id.in_(ids.values()))
                )
            ).all()
        )
        mismatches, wrong = [], []
        for name, pid in ids.items():
            pay = await s.get(Payment, pid)
            inv = await s.get(Invoice, pay.invoice_id)
            py_covered = (await payment_coverage(s, pay, inv.currency)).state == "covered"
            if bool(sql_verdict[pid]) != py_covered:
                mismatches.append(f"{name}: sql={sql_verdict[pid]} python={py_covered}")
            if py_covered != (name in EXPECTED_COVERED):
                wrong.append(f"{name}: covered={py_covered}")
    assert not mismatches, "\n".join(mismatches)
    assert not wrong, "\n".join(wrong)
