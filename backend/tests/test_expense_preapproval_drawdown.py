"""An approved pre-approval is a budget the lines it covers draw down
(decisions §563).

Before, the largest matching estimate covered each line on its own
(``covered >= amount``), so one "500 USD, travel" approval cleared the BLOCKING
``preapproval_required`` on any number of 400 USD lines across any number of
reports — the split-to-evade shape. Report submit now links each line that
needs cover to one pre-approval and records its draw under a row lock; reject
hands the draw back; a currency the estimate is not in is never covered.
"""

import asyncio
import uuid
from decimal import Decimal

from sqlalchemy import select, update

from app.models.expense import Expense


async def _policy(c, category: str, above: str = "100.00") -> None:
    resp = await c.post(
        "/api/expense-policies",
        json={"name": "Pre", "category": category, "requires_preapproval_above": above},
    )
    assert resp.status_code in (200, 201), resp.text


async def _approved(realdb, *, category: str, amount: str, currency: str = "USD") -> str:
    """The clerk raises a pre-approval; the manager approves it."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        pre = await c.post(
            "/api/expense-preapprovals",
            json={
                "title": "Trip",
                "estimated_amount": amount,
                "currency": currency,
                "category": category,
            },
        )
        assert pre.status_code == 201, pre.text
    async with realdb.client(key="a", role="ap_manager") as c:
        decided = await c.post(f"/api/expense-preapprovals/{pre.json()['id']}/approve")
        assert decided.status_code == 200, decided.text
    return pre.json()["id"]


async def _report(c, category: str, *amounts: str, currency: str = "USD"):
    """A clerk report holding one receipted line per amount (in ``currency``,
    and in a report of that currency, so no FX question arises)."""
    rid = (
        await c.post(
            "/api/expense-reports",
            json={"report_number": f"R-{uuid.uuid4().hex[:8]}", "currency": currency},
        )
    ).json()["id"]
    eids = []
    for amount in amounts:
        eid = (
            await c.post(
                "/api/expenses",
                json={
                    "expense_date": "2026-06-01",
                    "category": category,
                    "amount": amount,
                    "currency": currency,
                },
            )
        ).json()["id"]
        await c.post(
            f"/api/expenses/{eid}/receipt",
            files={"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        eids.append(eid)
    attach = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": eids})
    assert attach.status_code == 200, attach.text
    return rid, eids


def _money(value) -> Decimal | None:
    """Money fields serialise as JSON numbers (``schemas/money``)."""
    return None if value is None else Decimal(str(value))


def _codes(resp) -> set[str]:
    return {v["code"] for v in resp.json()["detail"]["violations"]}


async def _pre(c, pid: str) -> dict:
    resp = await c.get(f"/api/expense-preapprovals/{pid}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_one_estimate_no_longer_covers_unlimited_lines(realdb):
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _policy(c, category)
    pid = await _approved(realdb, category=category, amount="500.00")

    async with realdb.client(key="a", role="ap_clerk") as c:
        rid1, (eid1,) = await _report(c, category, "400.00")
        first = await c.post(f"/api/expense-reports/{rid1}/submit")
        assert first.status_code == 200, first.text
        line = (await c.get(f"/api/expenses/{eid1}")).json()
        assert line["preapproval_id"] == pid
        assert _money(line["preapproval_drawn_amount"]) == Decimal("400.00")
        pre = await _pre(c, pid)
        assert _money(pre["drawn_amount"]) == Decimal("400.00")
        assert _money(pre["remaining_amount"]) == Decimal("100.00")

        # The second 400 line is the one the old rule waved through.
        rid2, _ = await _report(c, category, "400.00")
        second = await c.post(f"/api/expense-reports/{rid2}/submit")
        assert second.status_code == 422, second.text
        assert _codes(second) == {"preapproval_required"}
        # A refused submit draws nothing.
        assert _money((await _pre(c, pid))["drawn_amount"]) == Decimal("400.00")

        # What is left still covers a line that fits it.
        rid3, (eid3, _) = await _report(c, category, "100.00", "50.00")
        # 100.00 is not ABOVE the 100.00 threshold, so neither line needs
        # cover and neither is charged to the budget.
        assert (await c.post(f"/api/expense-reports/{rid3}/submit")).status_code == 200
        assert (await c.get(f"/api/expenses/{eid3}")).json()["preapproval_id"] is None
        assert _money((await _pre(c, pid))["remaining_amount"]) == Decimal("100.00")


async def test_lines_on_one_report_share_the_estimate(realdb):
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _policy(c, category)
    pid = await _approved(realdb, category=category, amount="500.00")

    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eids = await _report(c, category, "300.00", "300.00")
        resp = await c.post(f"/api/expense-reports/{rid}/submit")
        assert resp.status_code == 422, resp.text
        assert _codes(resp) == {"preapproval_required"}
        # Exactly one of the two lines was the one left uncovered.
        assert len(resp.json()["detail"]["violations"]) == 1
        # The 422 rolled every draw back, including the one that fit.
        assert _money((await _pre(c, pid))["drawn_amount"]) == Decimal("0.00")
        for eid in eids:
            assert (await c.get(f"/api/expenses/{eid}")).json()["preapproval_id"] is None


async def test_reject_releases_the_draw_and_delete_leaves_none(realdb):
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _policy(c, category)
    pid = await _approved(realdb, category=category, amount="500.00")

    async with realdb.client(key="a", role="ap_clerk") as c:
        rid1, (eid1,) = await _report(c, category, "400.00")
        assert (await c.post(f"/api/expense-reports/{rid1}/submit")).status_code == 200
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.post(f"/api/expense-reports/{rid1}/reject")).status_code == 200
    async with realdb.client(key="a", role="ap_clerk") as c:
        line = (await c.get(f"/api/expenses/{eid1}")).json()
        assert line["status"] == "draft"
        assert line["preapproval_id"] is None
        assert line["preapproval_drawn_amount"] is None
        assert _money((await _pre(c, pid))["remaining_amount"]) == Decimal("500.00")

        # Released balance covers another claim…
        rid2, _ = await _report(c, category, "450.00")
        assert (await c.post(f"/api/expense-reports/{rid2}/submit")).status_code == 200
        assert _money((await _pre(c, pid))["remaining_amount"]) == Decimal("50.00")

        # …and deleting the rejected draft line changes nothing: it drew none.
        assert (await c.delete(f"/api/expenses/{eid1}")).status_code == 204
        assert _money((await _pre(c, pid))["remaining_amount"]) == Decimal("50.00")


async def test_a_draft_line_is_flagged_once_the_estimate_is_spent(realdb):
    """The advisory flags read the REMAINING balance, so a draft line written
    after the estimate is spent says ``preapproval_required`` before submit."""
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _policy(c, category)
    await _approved(realdb, category=category, amount="500.00")

    async with realdb.client(key="a", role="ap_clerk") as c:
        rid_before, (eid_before,) = await _report(c, category, "400.00")
        codes = {
            v["code"]
            for v in (await c.get(f"/api/expenses/{eid_before}")).json()["policy_violations"] or []
        }
        assert "preapproval_required" not in codes
        assert (await c.post(f"/api/expense-reports/{rid_before}/submit")).status_code == 200

        _, (eid_after,) = await _report(c, category, "400.00")
        codes = {
            v["code"]
            for v in (await c.get(f"/api/expenses/{eid_after}")).json()["policy_violations"] or []
        }
        assert "preapproval_required" in codes


async def test_a_currency_the_estimate_is_not_in_is_never_covered(realdb):
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _policy(c, category)
        # Thresholds in EUR so the EUR line compares without a rate.
        await c.post(
            "/api/expense-policies",
            json={
                "name": "Pre EUR",
                "category": category,
                "requires_preapproval_above": "100.00",
                "threshold_currency": "EUR",
            },
        )
    pid = await _approved(realdb, category=category, amount="5000.00", currency="USD")

    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _report(c, category, "400.00", currency="EUR")
        resp = await c.post(f"/api/expense-reports/{rid}/submit")
        assert resp.status_code == 422, resp.text
        assert "preapproval_required" in _codes(resp)
        pre = await _pre(c, pid)
        assert _money(pre["drawn_amount"]) == Decimal("0.00")
        assert _money(pre["remaining_amount"]) == Decimal("5000.00")


async def test_concurrent_submits_cannot_both_draw_the_last_remainder(realdb):
    """Two reports submitted at once, each for 400 against a 500 estimate: the
    row lock serialises them, so exactly one draws and the other is refused."""
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _policy(c, category)
    pid = await _approved(realdb, category=category, amount="500.00")

    async with realdb.client(key="a", role="ap_clerk") as c:
        rid1, _ = await _report(c, category, "400.00")
        rid2, _ = await _report(c, category, "400.00")
        r1, r2 = await asyncio.gather(
            c.post(f"/api/expense-reports/{rid1}/submit"),
            c.post(f"/api/expense-reports/{rid2}/submit"),
        )
        assert sorted([r1.status_code, r2.status_code]) == [200, 422], (r1.text, r2.text)
        loser = r1 if r1.status_code == 422 else r2
        assert _codes(loser) == {"preapproval_required"}
        pre = await _pre(c, pid)
        assert _money(pre["drawn_amount"]) == Decimal("400.00")
        assert _money(pre["remaining_amount"]) == Decimal("100.00")


async def test_a_line_with_no_recorded_draw_consumes_nothing(realdb):
    """No backfill: a line submitted before migration 0149 has no link, and
    the balance does not guess at one (§141)."""
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _policy(c, category)
    pid = await _approved(realdb, category=category, amount="500.00")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, (eid,) = await _report(c, category, "400.00")
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200

    # Stand the line where a pre-0149 submit left it: submitted, no link.
    async with realdb.sessionmaker("a")() as s:
        await s.execute(
            update(Expense)
            .where(Expense.id == uuid.UUID(eid))
            .values(preapproval_id=None, preapproval_drawn_amount=None)
        )
        await s.commit()
        row = (await s.execute(select(Expense).where(Expense.id == uuid.UUID(eid)))).scalar_one()
        assert str(row.status) == "submitted"

    async with realdb.client(key="a", role="ap_clerk") as c:
        assert _money((await _pre(c, pid))["remaining_amount"]) == Decimal("500.00")


async def test_list_shows_the_draw_down_and_pending_has_no_remaining(realdb):
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _policy(c, category)
    pid = await _approved(realdb, category=category, amount="500.00")
    async with realdb.client(key="a", role="ap_clerk") as c:
        pending = (
            await c.post(
                "/api/expense-preapprovals",
                json={"title": "Later", "estimated_amount": "80.00", "currency": "USD"},
            )
        ).json()
        assert _money(pending["drawn_amount"]) == Decimal("0.00")
        assert pending["remaining_amount"] is None
        rid, _ = await _report(c, category, "120.50")
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
        items = {
            p["id"]: p
            for p in (await c.get("/api/expense-preapprovals", params={"page_size": 100})).json()[
                "items"
            ]
        }
    assert _money(items[pid]["drawn_amount"]) == Decimal("120.50")
    assert _money(items[pid]["remaining_amount"]) == Decimal("379.50")
    assert items[pending["id"]]["remaining_amount"] is None
