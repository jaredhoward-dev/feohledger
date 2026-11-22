"""Live Stripe subscriptions behind a plan change (decisions §480).

Against an in-test, stateful fake of the Stripe REST endpoints the adapter
calls (an ``httpx.MockTransport`` — no network, no key; ``stripe-mock`` is
stateless, so it cannot show a subscription created by one call being amended
by the next). Three groups:

* the adapter's wire shape — create anchored on the 1st with the metered item,
  ``error_if_incomplete``, the pinned ``Stripe-Version``; item swap by
  ``usage_type``; cancel with ``invoice_now`` + ``prorate``; a 402 is a
  decline, not an outage; SetupIntent adoption refuses a foreign or unfinished
  intent; webhook parsing reads an invoice's subscription from ``parent``;
* ``change_plan`` on the real-Postgres harness for every path — free → paid
  (no card refused, card → created), paid → paid (updated), paid → free
  (canceled, id cleared), a decline changing nothing, adopting a subscription a
  dead attempt left behind, the per-org lease, and the deployed + ``mock``
  refusal (with the downgrade still allowed);
* the webhook keeping our row in sync — invoice events move status only along
  their narrow edges, the provider's window is copied, ``canceled`` is terminal
  — and the add-card adoption endpoint.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.billing import Plan, Subscription
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services.billing.plan_change import change_plan
from app.services.billing.plan_preview import PlanChangeQuote, preview_plan_change
from app.services.billing.provider_sync import (
    LEASE_KEY,
    ProviderLegRefused,
    next_month_start,
)
from app.services.billing.webhook_processing import apply_billing_event
from app.services.billing_adapters.base import (
    BillingPaymentDeclined,
    BillingProviderError,
    BillingWebhookEvent,
    CreateSubscriptionRequest,
    PlanChangePreviewRequest,
    SetupIntentRejected,
)
from app.services.billing_adapters.stripe_billing import StripeBillingAdapter

PREFIX = "sbtest_"
OVERAGE = {"ai_invoices": {"included": 500, "overage_unit_price": "0.10"}}
CHANGE_AT = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# A stateful fake of the slice of Stripe the adapter calls.
# ---------------------------------------------------------------------------


class FakeStripe:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.customers: dict[str, dict] = {}
        self.cards: dict[str, list[str]] = {}  # customer -> pm ids, newest first
        self.subscriptions: dict[str, dict] = {}
        self.setup_intents: dict[str, dict] = {}
        self.prices: dict[str, str] = {}  # lookup_key -> price id
        self.replies: dict[str, tuple[int, dict]] = {}  # Idempotency-Key -> answer
        self.decline = False
        # What `POST /v1/invoices/create_preview` answers, in minor units, per
        # kind of change previewed (a test moves one to make a quote stale).
        self.preview_due = {"create": 1480, "update": 7500, "cancel": 0}
        self.preview_total = {"create": 1480, "update": 7500, "cancel": -1500}
        self.recurring_total = 4900
        self.preview_currency: str | None = "usd"
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n}"

    def add_card(self, customer: str, *, default: bool = True) -> str:
        pm = self._id("pm")
        self.cards.setdefault(customer, []).insert(0, pm)
        if default:
            self.customer(customer)["invoice_settings"]["default_payment_method"] = pm
        return pm

    def customer(self, cid: str) -> dict:
        return self.customers.setdefault(
            cid, {"id": cid, "invoice_settings": {"default_payment_method": None}}
        )

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    @staticmethod
    def form(request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def _sub_json(self, sub: dict) -> dict:
        items = []
        for item in sub["items"]:
            metered = item["price"].startswith("price_over")
            items.append(
                {
                    "id": item["id"],
                    "price": {
                        "id": item["price"],
                        "recurring": {"usage_type": "metered" if metered else "licensed"},
                    },
                    "current_period_start": sub["period"][0],
                    "current_period_end": sub["period"][1],
                }
            )
        return {
            "id": sub["id"],
            "status": sub["status"],
            "customer": sub["customer"],
            "metadata": sub["metadata"],
            "items": {"data": items},
            "cancel_at_period_end": sub.get("cancel_at_period_end", False),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Stripe's idempotency, as the decline-then-retry path depends on it:
        a request whose `Idempotency-Key` was seen before gets the stored
        answer replayed — a 402 included — without being executed again."""
        self.requests.append(request)
        key = request.headers.get("Idempotency-Key")
        if key and key in self.replies:
            status, body = self.replies[key]
            return httpx.Response(status, json=body)
        response = self._route(request)
        if key:
            self.replies[key] = (response.status_code, response.json())
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:  # noqa: C901 — one router
        path, method = request.url.path, request.method
        if method == "POST" and path == "/v1/customers":
            cid = self._id("cus")
            self.customer(cid)
            return httpx.Response(200, json={"id": cid})
        if path.startswith("/v1/customers/"):
            cid = path.rsplit("/", 1)[1]
            if method == "POST":
                pm = self.form(request)["invoice_settings[default_payment_method]"]
                self.customer(cid)["invoice_settings"]["default_payment_method"] = pm
            return httpx.Response(200, json=self.customer(cid))
        if path == "/v1/prices":
            if method == "GET":
                found = self.prices.get(request.url.params["lookup_keys[]"])
                return httpx.Response(200, json={"data": [{"id": found}] if found else []})
            form = self.form(request)
            metered = "recurring[usage_type]" in form
            price = self._id("price_over" if metered else "price")
            self.prices[form["lookup_key"]] = price
            return httpx.Response(200, json={"id": price})
        if path == "/v1/billing/meters":
            if method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"id": "mtr_1"})
        if method == "POST" and path == "/v1/invoices/create_preview":
            form = self.form(request)
            if form.get("preview_mode") == "recurring":
                due = total = self.recurring_total
            else:
                if form.get("subscription_details[cancel_now]") == "true":
                    kind = "cancel"
                elif "subscription" in form:
                    kind = "update"
                else:
                    kind = "create"
                due, total = self.preview_due[kind], self.preview_total[kind]
            return httpx.Response(
                200,
                json={
                    "id": "upcoming_in_1",
                    "amount_due": due,
                    "total": total,
                    "currency": self.preview_currency,
                },
            )
        if method == "GET" and path == "/v1/payment_methods":
            cid = request.url.params["customer"]
            data = [
                {"id": pm, "card": {"brand": "visa", "last4": "4242"}}
                for pm in self.cards.get(cid, [])
            ]
            return httpx.Response(200, json={"data": data})
        if method == "GET" and path.startswith("/v1/setup_intents/"):
            seti = self.setup_intents.get(path.rsplit("/", 1)[1])
            if seti is None:
                return httpx.Response(404, json={"error": {"type": "invalid_request_error"}})
            return httpx.Response(200, json=seti)
        if path == "/v1/subscriptions":
            if method == "GET":
                cid = request.url.params["customer"]
                data = [
                    self._sub_json(s) for s in self.subscriptions.values() if s["customer"] == cid
                ]
                return httpx.Response(200, json={"data": data})
            if self.decline:
                return httpx.Response(402, json={"error": {"type": "card_error"}})
            form = self.form(request)
            anchor = int(form["billing_cycle_anchor"])
            sid = self._id("sub")
            items = [{"id": self._id("si"), "price": form["items[0][price]"]}]
            if "items[1][price]" in form:
                items.append({"id": self._id("si"), "price": form["items[1][price]"]})
            self.subscriptions[sid] = {
                "id": sid,
                "status": "active",
                "customer": form["customer"],
                "metadata": {"plan_code": form["metadata[plan_code]"]},
                "items": items,
                "period": (int(CHANGE_AT.timestamp()), anchor),
            }
            return httpx.Response(200, json=self._sub_json(self.subscriptions[sid]))
        if path.startswith("/v1/subscriptions/"):
            sub = self.subscriptions.get(path.rsplit("/", 1)[1])
            if sub is None:
                return httpx.Response(404, json={"error": {}})
            if method == "GET":
                return httpx.Response(200, json=self._sub_json(sub))
            if method == "DELETE":
                if sub["status"] == "canceled":
                    return httpx.Response(400, json={"error": {}})
                sub["status"] = "canceled"
                return httpx.Response(200, json=self._sub_json(sub))
            form = self.form(request)
            if sub["status"] == "canceled":
                # Stripe: a canceled subscription can't be updated (bar metadata).
                return httpx.Response(400, json={"error": {"type": "invalid_request_error"}})
            if "cancel_at_period_end" in form:
                sub["cancel_at_period_end"] = form["cancel_at_period_end"] == "true"
            if not any(k.startswith("items[") for k in form):
                # Only the scheduled end moved: nothing to charge.
                return httpx.Response(200, json=self._sub_json(sub))
            if self.decline:
                return httpx.Response(402, json={"error": {"type": "card_error"}})
            by_id = {i["id"]: i for i in sub["items"]}
            for n in range(4):
                item_id = form.get(f"items[{n}][id]")
                price = form.get(f"items[{n}][price]")
                if item_id and form.get(f"items[{n}][deleted]") == "true":
                    sub["items"] = [i for i in sub["items"] if i["id"] != item_id]
                elif item_id and price:
                    by_id[item_id]["price"] = price
                elif price:
                    sub["items"].append({"id": self._id("si"), "price": price})
            sub["metadata"]["plan_code"] = form.get("metadata[plan_code]", "")
            return httpx.Response(200, json=self._sub_json(sub))
        return httpx.Response(500, json={"error": {"message": f"unrouted {method} {path}"}})


@pytest.fixture
def fake_stripe(monkeypatch):
    """Route every StripeBillingAdapter through the fake, with a fake key."""
    fake = FakeStripe()
    transport = httpx.MockTransport(fake.handler)

    def _client(self):
        return httpx.AsyncClient(
            base_url="https://api.stripe.test",
            auth=(self._api_key, ""),
            transport=transport,
            headers={"Stripe-Version": self._api_version},
        )

    monkeypatch.setattr(StripeBillingAdapter, "_client", _client)
    monkeypatch.setattr(settings, "billing_provider", "stripe_billing")
    monkeypatch.setattr(settings, "billing_stripe_api_key", "sk_test_fake_not_a_key")
    return fake


def _adapter(fake: FakeStripe, **config) -> StripeBillingAdapter:
    adapter = StripeBillingAdapter(
        {
            "stripe_api_key": "sk_test_fake_not_a_key",
            "stripe_api_version": "2026-11-18.endive",
            **config,
        }
    )
    return adapter


# ---------------------------------------------------------------------------
# Adapter wire shape — no DB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_anchors_on_the_first_with_the_metered_item(fake_stripe):
    adapter = _adapter(
        fake_stripe,
        stripe_customer_id="cus_x",
        stripe_price_id="price_base",
        stripe_overage_price_id="price_over_1",
    )
    anchor = next_month_start(CHANGE_AT)
    sub = await adapter.create_subscription(
        CreateSubscriptionRequest(
            organization_id="org-1",
            plan_code="growth",
            monthly_price=Decimal("49.00"),
            idempotency_key="feohledger-sub-create-k",
            billing_cycle_anchor=anchor,
        )
    )
    (request,) = fake_stripe.calls("POST", "/v1/subscriptions")
    form = FakeStripe.form(request)
    assert anchor == datetime(2026, 7, 1, tzinfo=UTC)
    assert form["billing_cycle_anchor"] == str(int(anchor.timestamp()))
    assert form["proration_behavior"] == "create_prorations"
    assert form["payment_behavior"] == "error_if_incomplete"
    assert form["items[0][price]"] == "price_base"
    assert form["items[1][price]"] == "price_over_1"
    assert request.headers["Idempotency-Key"] == "feohledger-sub-create-k"
    assert request.headers["Stripe-Version"] == "2026-11-18.endive"
    # The window is read from the item (API 2025-03-31+), not the top level.
    assert sub.current_period_end == anchor
    assert sub.status == "active"


def test_next_month_start_rolls_the_year():
    assert next_month_start(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )
    # Exactly on the 1st still anchors in the FUTURE (Stripe refuses a past anchor).
    assert next_month_start(datetime(2026, 6, 1, tzinfo=UTC)) == datetime(2026, 7, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_dispatcher_pins_the_stripe_api_version(monkeypatch):
    from app.services.billing_adapters import get_billing_adapter

    captured = {}

    def handler(request):
        captured["version"] = request.headers.get("Stripe-Version")
        return httpx.Response(200, json={"id": "sub_1", "status": "active"})

    adapter = get_billing_adapter("stripe_billing")
    adapter.config = {**adapter.config, "stripe_api_key": "sk_test_fake_not_a_key"}
    real_client = adapter._client

    def _client():
        client = real_client()
        client._transport = httpx.MockTransport(handler)
        return client

    adapter._client = _client  # type: ignore[method-assign]
    await adapter.get_subscription("sub_1")
    assert captured["version"] == settings.billing_stripe_api_version
    assert settings.billing_stripe_api_version  # pinned, not the account default


@pytest.mark.asyncio
async def test_change_swaps_items_by_usage_type_and_invoices_now(fake_stripe):
    fake_stripe.subscriptions["sub_9"] = {
        "id": "sub_9",
        "status": "active",
        "customer": "cus_x",
        "metadata": {},
        "items": [{"id": "si_base", "price": "price_a"}, {"id": "si_met", "price": "price_over_a"}],
        "period": (0, 1),
    }
    adapter = _adapter(fake_stripe)
    await adapter.change_subscription_plan(
        "sub_9",
        price_id="price_b",
        overage_price_id="price_over_b",
        plan_code="scale",
        idempotency_key="k1",
    )
    form = FakeStripe.form(fake_stripe.calls("POST", "/v1/subscriptions/sub_9")[0])
    assert form["items[0][id]"] == "si_base" and form["items[0][price]"] == "price_b"
    assert form["items[1][id]"] == "si_met" and form["items[1][price]"] == "price_over_b"
    assert form["proration_behavior"] == "always_invoice"
    assert form["payment_behavior"] == "error_if_incomplete"

    # A plan with no overage drops the metered item rather than leaving it billing.
    await adapter.change_subscription_plan(
        "sub_9", price_id="price_c", overage_price_id=None, plan_code="flat", idempotency_key="k2"
    )
    form = FakeStripe.form(fake_stripe.calls("POST", "/v1/subscriptions/sub_9")[1])
    assert form["items[1][deleted]"] == "true"
    assert [i["price"] for i in fake_stripe.subscriptions["sub_9"]["items"]] == ["price_c"]


@pytest.mark.asyncio
async def test_cancel_invoices_usage_now_and_is_idempotent(fake_stripe):
    fake_stripe.subscriptions["sub_c"] = {
        "id": "sub_c",
        "status": "active",
        "customer": "cus_x",
        "metadata": {},
        "items": [{"id": "si", "price": "price_a"}],
        "period": (0, 1),
    }
    adapter = _adapter(fake_stripe)
    first = await adapter.cancel_subscription("sub_c")
    (delete,) = fake_stripe.calls("DELETE", "/v1/subscriptions/sub_c")
    assert dict(delete.url.params) == {"invoice_now": "true", "prorate": "true"}
    assert first.status == "canceled"
    # A second cancel answers with the canceled subscription, not an error.
    again = await adapter.cancel_subscription("sub_c")
    assert again.status == "canceled"


@pytest.mark.asyncio
async def test_a_402_is_a_decline_not_an_outage(fake_stripe):
    fake_stripe.decline = True
    adapter = _adapter(fake_stripe, stripe_customer_id="cus_x", stripe_price_id="price_a")
    with pytest.raises(BillingPaymentDeclined):
        await adapter.create_subscription(
            CreateSubscriptionRequest(
                organization_id="o", plan_code="g", monthly_price=Decimal("1")
            )
        )


@pytest.mark.asyncio
async def test_default_card_is_promoted_from_the_newest_saved_card(fake_stripe):
    adapter = _adapter(fake_stripe)
    assert await adapter.ensure_default_payment_method("cus_none") is None
    pm = fake_stripe.add_card("cus_y", default=False)
    assert await adapter.ensure_default_payment_method("cus_y") == pm
    assert fake_stripe.customer("cus_y")["invoice_settings"]["default_payment_method"] == pm


@pytest.mark.asyncio
async def test_setup_intent_adoption_refuses_foreign_and_unfinished(fake_stripe):
    adapter = _adapter(fake_stripe)
    fake_stripe.setup_intents["seti_ok"] = {
        "id": "seti_ok",
        "customer": "cus_me",
        "status": "succeeded",
        "payment_method": "pm_new",
    }
    fake_stripe.setup_intents["seti_other"] = {
        "id": "seti_other",
        "customer": "cus_them",
        "status": "succeeded",
        "payment_method": "pm_x",
    }
    fake_stripe.setup_intents["seti_wip"] = {
        "id": "seti_wip",
        "customer": "cus_me",
        "status": "requires_payment_method",
        "payment_method": None,
    }
    assert await adapter.adopt_setup_intent("cus_me", "seti_ok") == "pm_new"
    assert fake_stripe.customer("cus_me")["invoice_settings"]["default_payment_method"] == "pm_new"
    for seti, reason in (
        ("seti_other", "not_found"),
        ("seti_missing", "not_found"),
        ("seti_wip", "incomplete"),
        ("pi_not_a_seti", "not_found"),
        # Never interpolated into a path: this would GET /v1/customers/cus_x.
        ("seti_/../../customers/cus_x", "not_found"),
    ):
        with pytest.raises(SetupIntentRejected) as exc:
            await adapter.adopt_setup_intent("cus_me", seti)
        assert exc.value.reason == reason
    # Another customer's card was never made ours.
    assert fake_stripe.customer("cus_me")["invoice_settings"]["default_payment_method"] == "pm_new"


# ---------------------------------------------------------------------------
# Preview — POST /v1/invoices/create_preview (decisions §482).
# ---------------------------------------------------------------------------


def _preview_request(**overrides) -> PlanChangePreviewRequest:
    base = {
        "new_plan_code": "growth",
        "to_paid": True,
        "old_monthly_price": Decimal("0.00"),
        "new_monthly_price": Decimal("49.00"),
        "currency": "USD",
        "proration_date": CHANGE_AT,
        "period_start": datetime(2026, 6, 1, tzinfo=UTC),
        "period_end": datetime(2026, 7, 1, tzinfo=UTC),
        "customer_id": "cus_org",
        "price_id": "price_base",
        "overage_price_id": "price_over_1",
    }
    return PlanChangePreviewRequest(**{**base, **overrides})


@pytest.mark.asyncio
async def test_preview_of_a_new_subscription_prices_the_create_it_will_make(fake_stripe):
    adapter = _adapter(fake_stripe)
    anchor = next_month_start(CHANGE_AT)
    preview = await adapter.preview_plan_change(_preview_request(billing_cycle_anchor=anchor))

    first, recurring = fake_stripe.calls("POST", "/v1/invoices/create_preview")
    # The same items, proration and anchor the create sends — the anchor in the
    # endive preview's object form.
    assert FakeStripe.form(first) == {
        "customer": "cus_org",
        "subscription_details[items][0][price]": "price_base",
        "subscription_details[items][1][price]": "price_over_1",
        "subscription_details[proration_behavior]": "create_prorations",
        "subscription_details[billing_cycle_anchor][type]": "timestamp",
        "subscription_details[billing_cycle_anchor][timestamp]": str(int(anchor.timestamp())),
    }
    assert FakeStripe.form(recurring) == {
        "customer": "cus_org",
        "subscription_details[items][0][price]": "price_base",
        "subscription_details[items][1][price]": "price_over_1",
        "preview_mode": "recurring",
    }
    assert first.headers["Stripe-Version"] == "2026-11-18.endive"
    # Nothing created: a preview is never a charge.
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    assert preview.amount_due == Decimal("14.80")
    assert preview.currency == "USD"
    assert preview.next_amount == Decimal("49.00")
    assert preview.next_date == anchor


@pytest.mark.asyncio
async def test_preview_of_an_amendment_prices_the_swap_as_of_its_proration_date(fake_stripe):
    _live_sub(fake_stripe, "sub_live")
    fake_stripe.subscriptions["sub_live"]["items"].append({"id": "si_m", "price": "price_over_0"})
    adapter = _adapter(fake_stripe)
    preview = await adapter.preview_plan_change(
        _preview_request(
            external_subscription_id="sub_live",
            old_monthly_price=Decimal("49.00"),
            new_monthly_price=Decimal("199.00"),
            price_id="price_scale",
            overage_price_id="price_over_2",
        )
    )
    first, recurring = fake_stripe.calls("POST", "/v1/invoices/create_preview")
    swap = {
        "subscription_details[items][0][id]": "si_sub_live",
        "subscription_details[items][0][price]": "price_scale",
        "subscription_details[items][1][id]": "si_m",
        "subscription_details[items][1][price]": "price_over_2",
    }
    assert FakeStripe.form(first) == {
        "subscription": "sub_live",
        **swap,
        "subscription_details[proration_behavior]": "always_invoice",
        "subscription_details[proration_date]": str(int(CHANGE_AT.timestamp())),
        # A paid target keeps renewing: priced as the amend sends it (§483).
        "subscription_details[cancel_at_period_end]": "false",
    }
    assert FakeStripe.form(recurring) == {
        "subscription": "sub_live",
        **swap,
        "subscription_details[proration_behavior]": "none",
        "preview_mode": "recurring",
        "subscription_details[cancel_at_period_end]": "false",
    }
    assert preview.amount_due == Decimal("75.00")
    assert preview.next_amount == Decimal("49.00")
    # From the subscription item's own window, not ours.
    assert preview.next_date == datetime(2026, 7, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_preview_of_a_move_to_free_prices_the_cancel_and_its_credit(fake_stripe):
    _live_sub(fake_stripe, "sub_live")
    adapter = _adapter(fake_stripe)
    preview = await adapter.preview_plan_change(
        _preview_request(
            to_paid=False,
            external_subscription_id="sub_live",
            new_monthly_price=Decimal("0.00"),
            price_id="price_free",
            overage_price_id=None,
        )
    )
    (only,) = fake_stripe.calls("POST", "/v1/invoices/create_preview")
    assert FakeStripe.form(only) == {
        "subscription": "sub_live",
        "subscription_details[cancel_now]": "true",
        "subscription_details[proration_behavior]": "create_prorations",
    }
    assert preview.amount_due == Decimal("0.00")
    assert preview.total == Decimal("-15.00")  # unused time credited
    assert preview.next_amount is None and preview.next_date is None
    assert fake_stripe.subscriptions["sub_live"]["status"] == "active"  # not canceled


@pytest.mark.asyncio
async def test_a_preview_answer_without_an_amount_is_an_error_not_zero(fake_stripe):
    fake_stripe.preview_due["create"] = None
    adapter = _adapter(fake_stripe)
    with pytest.raises(BillingProviderError):
        await adapter.preview_plan_change(_preview_request())


@pytest.mark.asyncio
async def test_a_preview_answer_without_a_currency_is_an_error_not_the_plans(fake_stripe):
    fake_stripe.preview_currency = None
    adapter = _adapter(fake_stripe)
    with pytest.raises(BillingProviderError):
        await adapter.preview_plan_change(_preview_request())


@pytest.mark.asyncio
async def test_the_amendment_is_prorated_as_of_the_confirmed_instant(fake_stripe):
    _live_sub(fake_stripe, "sub_live")
    adapter = _adapter(fake_stripe)
    await adapter.change_subscription_plan(
        "sub_live",
        price_id="price_scale",
        overage_price_id=None,
        plan_code="scale",
        idempotency_key="k",
        proration_date=CHANGE_AT,
    )
    (update,) = fake_stripe.calls("POST", "/v1/subscriptions/sub_live")
    assert FakeStripe.form(update)["proration_date"] == str(int(CHANGE_AT.timestamp()))


def _signed(adapter: StripeBillingAdapter, payload: dict) -> tuple[dict, bytes]:
    import hashlib
    import hmac
    import time

    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    digest = hmac.new(b"whsec_fake", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={t},v1={digest}"}, body


def test_parse_webhook_reads_an_invoices_subscription_from_parent():
    adapter = StripeBillingAdapter({"stripe_webhook_secret": "whsec_fake"})
    headers, body = _signed(
        adapter,
        {
            "id": "evt_inv",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_1",
                    "status": "open",
                    "parent": {"subscription_details": {"subscription": "sub_42"}},
                }
            },
        },
    )
    event = adapter.parse_webhook(headers, body)
    # The INVOICE's id and status are not the subscription's.
    assert event.external_subscription_id == "sub_42"
    assert event.status is None
    assert event.event_type == "invoice.payment_failed"


def test_parse_webhook_carries_the_subscription_window():
    adapter = StripeBillingAdapter({"stripe_webhook_secret": "whsec_fake"})
    start, end = 1782864000, 1785542400  # 2026-07-01, 2026-08-01 UTC
    headers, body = _signed(
        adapter,
        {
            "id": "evt_sub",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_42",
                    "status": "active",
                    "items": {"data": [{"current_period_start": start, "current_period_end": end}]},
                }
            },
        },
    )
    event = adapter.parse_webhook(headers, body)
    assert event.status == "active"
    assert event.current_period_start == datetime(2026, 7, 1, tzinfo=UTC)
    assert event.current_period_end == datetime(2026, 8, 1, tzinfo=UTC)


def test_parse_webhook_ignores_other_objects_statuses():
    adapter = StripeBillingAdapter({"stripe_webhook_secret": "whsec_fake"})
    headers, body = _signed(
        adapter,
        {
            "id": "evt_pi",
            "type": "payment_intent.canceled",
            "data": {"object": {"id": "pi_1", "status": "canceled"}},
        },
    )
    event = adapter.parse_webhook(headers, body)
    assert event.external_subscription_id is None and event.status is None


# ---------------------------------------------------------------------------
# Real-DB: change_plan against the fake.
# ---------------------------------------------------------------------------


@pytest.fixture
def _audit_engine_on_loop(monkeypatch, realdb):
    """Write dispatch_auth_audit's tenant row on THIS test's loop (see
    test_billing_webhook.py for the full rationale)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.database import _make_tenant_url

    db_name_by_org = {info.org_id: info.db_name for info in realdb.tenants.values()}

    def _engine(db_name: str):
        return create_async_engine(_make_tenant_url(db_name), poolclass=NullPool)

    async def _resolve(organization_id):
        return db_name_by_org[organization_id]

    monkeypatch.setattr("app.services.audit_dispatch._resolve_tenant_db_name", _resolve)
    monkeypatch.setattr("app.database.get_tenant_engine", _engine)


@pytest.fixture
async def org_a(realdb):
    """Org "a" with no billing block; cleaned up (plans, subs, settings) after."""
    org_id = realdb.info("a").org_id
    await _reset(realdb, org_id)
    yield org_id
    await _reset(realdb, org_id)


async def _reset(realdb, org_id):
    await realdb.purge_plans(PREFIX, org_ids=[org_id])
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        if "billing" in (org.settings or {}):
            new_settings = dict(org.settings)
            new_settings.pop("billing", None)
            org.settings = new_settings
            flag_modified(org, "settings")
        await s.commit()


async def _plan(realdb, code, price, *, overage=True):
    async with realdb.control_sessionmaker()() as s:
        plan = Plan(
            id=uuid.uuid4(),
            code=f"{PREFIX}{code}",
            name=code.title(),
            monthly_price=Decimal(price),
            currency="USD",
            usage_components=OVERAGE
            if overage
            else {"ai_invoices": {"included": 100, "overage_unit_price": None}},
            entitlements={},
            trial_days=14,
        )
        s.add(plan)
        await s.commit()
        return plan.id


async def _sub(realdb, org_id, plan_id, *, external_id=None, status="active"):
    async with realdb.control_sessionmaker()() as s:
        sub = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            plan_id=plan_id,
            status=status,
            current_period_start=datetime(2026, 6, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 7, 1, tzinfo=UTC),
            external_subscription_id=external_id,
        )
        s.add(sub)
        await s.commit()
        return sub.id


async def _set_billing(realdb, org_id, **values):
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        new_settings = dict(org.settings or {})
        new_settings["billing"] = {**(new_settings.get("billing") or {}), **values}
        org.settings = new_settings
        flag_modified(org, "settings")
        await s.commit()


async def _row(realdb, sub_id) -> Subscription:
    async with realdb.control_sessionmaker()() as s:
        return (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()


async def _org_billing(realdb, org_id) -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        return dict((org.settings or {}).get("billing") or {})


async def _preview(realdb, org_id, code, *, at=CHANGE_AT):
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        return await preview_plan_change(s, org=org, new_plan_code=f"{PREFIX}{code}", now=at)


def _quote_of(preview) -> PlanChangeQuote:
    return PlanChangeQuote(
        proration_date=preview.proration_date,
        amount_due=preview.amount_due,
        currency=preview.currency,
    )


async def _change(realdb, org_id, code, *, at=CHANGE_AT, quote=None):
    """Preview, then confirm what the preview showed — the only way a live
    change is accepted (decisions §482). ``quote`` overrides what is confirmed."""
    if quote is None:
        quote = _quote_of(await _preview(realdb, org_id, code, at=at))
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        return await change_plan(
            s, org=org, new_plan_code=f"{PREFIX}{code}", actor_id=None, change_at=at, quote=quote
        )


async def _post_change(c, code: str):
    """The SPA's two calls: preview, then confirm with the figures it showed.
    A refused preview is answered as is — the SPA never gets to confirm."""
    preview = await c.post("/api/billing/change-plan/preview", json={"plan_code": code})
    if preview.status_code != 200:
        return preview
    body = preview.json()
    return await c.post(
        "/api/billing/change-plan",
        json={
            "plan_code": code,
            "proration_date": body["proration_date"],
            "expected_amount_due": body["amount_due"],
            "currency": body["currency"],
        },
    )


async def _audit_details(realdb, sub_id, *, action="billing.plan_changed") -> list[dict]:
    async with realdb.sessionmaker("a")() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog.details)
                    .where(AuditLog.action == action, AuditLog.entity_id == sub_id)
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


@pytest.mark.asyncio
async def test_free_to_paid_without_a_card_is_refused_and_changes_nothing(
    realdb, fake_stripe, org_a
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)

    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth")
    assert exc.value.code == "payment_method_required"
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.external_subscription_id is None
    # The lease is released, so the retry after adding a card is not blocked.
    assert LEASE_KEY not in await _org_billing(realdb, org_a)


@pytest.mark.asyncio
async def test_endpoint_answers_payment_method_required_as_a_coded_409(realdb, fake_stripe, org_a):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    await _sub(realdb, org_a, free)
    async with realdb.client(key="a", role="admin") as c:
        resp = await _post_change(c, f"{PREFIX}growth")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "payment_method_required"


@pytest.mark.asyncio
async def test_free_to_paid_creates_the_stripe_subscription(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")

    result = await _change(realdb, org_a, "growth")

    assert result.changed is True
    (create,) = fake_stripe.calls("POST", "/v1/subscriptions")
    form = FakeStripe.form(create)
    assert form["customer"] == "cus_org"
    assert form["items[1][price]"].startswith("price_over")  # metered overage item
    assert "trial_period_days" not in form  # an upgrade from Free is not a trial
    row = await _row(realdb, sub_id)
    assert row.external_subscription_id == next(iter(fake_stripe.subscriptions))
    assert row.status == "active"
    # The provider's window is what the row now carries: now → the 1st.
    assert row.current_period_end == datetime(2026, 7, 1, tzinfo=UTC)
    # Our estimate is over the calendar month the provider prorates in:
    # 49.00 * 14 / 30 (June 16 12:00 → July 1, floored days).
    assert result.proration.period_days == 30
    assert result.proration.unused_days == 14
    assert result.proration.amount == Decimal("22.87")
    (details,) = await _audit_details(realdb, sub_id)
    assert details["provider"] == "stripe_billing"
    assert details["provider_action"] == "created"
    assert details["external_subscription_id"] == row.external_subscription_id
    # What the customer confirmed is on the trail beside our estimate.
    assert details["confirmed_amount_due"] == "14.80"
    assert details["confirmed_currency"] == "USD"
    assert details["quote_proration_date"] == CHANGE_AT.isoformat()


@pytest.mark.asyncio
async def test_paid_to_paid_updates_items_and_paid_to_free_schedules_the_end(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    scale = await _plan(realdb, "scale", "199.00")
    sub_id = await _sub(realdb, org_a, growth)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    fake_stripe.subscriptions["sub_live"] = {
        "id": "sub_live",
        "status": "active",
        "customer": "cus_org",
        "metadata": {},
        "items": [{"id": "si_b", "price": "price_old"}, {"id": "si_m", "price": "price_over_old"}],
        "period": (
            int(datetime(2026, 6, 1, tzinfo=UTC).timestamp()),
            int(datetime(2026, 7, 1, tzinfo=UTC).timestamp()),
        ),
    }
    async with realdb.control_sessionmaker()() as s:
        row = (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
        row.external_subscription_id = "sub_live"
        await s.commit()

    up = await _change(realdb, org_a, "scale")
    assert up.changed is True
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []  # amended, not created
    (update,) = fake_stripe.calls("POST", "/v1/subscriptions/sub_live")
    assert FakeStripe.form(update)["items[0][id]"] == "si_b"
    # Prorated as of the instant the confirmed preview was priced at.
    assert FakeStripe.form(update)["proration_date"] == str(int(CHANGE_AT.timestamp()))
    # Every paid amend says "keep renewing" — a no-op when nothing is set to
    # end, and the fix when Stripe is set to end it and our row does not know.
    assert FakeStripe.form(update)["cancel_at_period_end"] == "false"
    assert (await _row(realdb, sub_id)).external_subscription_id == "sub_live"

    # Paid → free keeps the paid period (decisions §483): the end is scheduled
    # at Stripe, nothing is canceled, prorated or credited now.
    down = await _change(realdb, org_a, "free", at=CHANGE_AT + timedelta(days=1))
    assert down.changed is True
    assert down.scheduled is True
    assert down.effective_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert down.proration.amount == Decimal("0.00")
    assert fake_stripe.calls("DELETE", "/v1/subscriptions/sub_live") == []
    schedule = fake_stripe.calls("POST", "/v1/subscriptions/sub_live")[1]
    assert FakeStripe.form(schedule) == {
        "cancel_at_period_end": "true",
        "proration_behavior": "none",
    }
    assert schedule.headers["Idempotency-Key"].startswith("feohledger-sub-schedule-free-")
    assert fake_stripe.subscriptions["sub_live"]["status"] == "active"
    row = await _row(realdb, sub_id)
    # Still on the paid plan, entitled and billed by the same subscription.
    assert row.plan_id == scale and row.status == "active"
    assert row.external_subscription_id == "sub_live"
    assert row.pending_plan_id == free
    assert row.pending_plan_effective_at == datetime(2026, 7, 1, tzinfo=UTC)
    actions = [d["provider_action"] for d in await _audit_details(realdb, sub_id)]
    assert actions == ["updated"]
    (scheduled,) = await _audit_details(realdb, sub_id, action="billing.plan_change_scheduled")
    assert scheduled["from_plan"] == f"{PREFIX}scale"
    assert scheduled["to_plan"] == f"{PREFIX}free"
    assert scheduled["effective_at"] == "2026-07-01T00:00:00+00:00"
    assert scheduled["proration_amount"] == "0.00"
    assert scheduled["provider_action"] == "cancel_at_period_end"


@pytest.mark.asyncio
async def test_a_declined_upgrade_changes_nothing(realdb, fake_stripe, org_a):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    fake_stripe.decline = True

    async with realdb.client(key="a", role="admin") as c:
        resp = await _post_change(c, f"{PREFIX}growth")
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["code"] == "billing_payment_declined"
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.external_subscription_id is None
    assert fake_stripe.subscriptions == {}


@pytest.mark.asyncio
async def test_after_a_decline_a_new_card_is_charged_not_the_replayed_decline(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    """Stripe replays a stored 402 for the same Idempotency-Key for 24 h, and a
    decline changes nothing on our row — so the key must change with the card,
    or the customer who replaced it would be refused again."""
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    fake_stripe.decline = True
    with pytest.raises(BillingPaymentDeclined):
        await _change(realdb, org_a, "growth")

    # Same card, issuer now approves: still the replayed decline (Stripe's
    # behaviour, not ours to change) — the remedy the UI offers is a new card.
    fake_stripe.decline = False
    with pytest.raises(BillingPaymentDeclined):
        await _change(realdb, org_a, "growth")

    fake_stripe.add_card("cus_org")  # the customer replaces the card
    result = await _change(realdb, org_a, "growth")
    assert result.changed is True
    assert (await _row(realdb, sub_id)).external_subscription_id in fake_stripe.subscriptions


@pytest.mark.asyncio
async def test_a_subscription_a_dead_attempt_left_is_adopted_not_duplicated(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    # Stripe created it; our commit never happened.
    fake_stripe.subscriptions["sub_orphan"] = {
        "id": "sub_orphan",
        "status": "active",
        "customer": "cus_org",
        "metadata": {},
        "items": [{"id": "si_b", "price": "price_x"}],
        "period": (0, 1),
    }

    await _change(realdb, org_a, "growth")

    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    assert len(fake_stripe.subscriptions) == 1
    assert (await _row(realdb, sub_id)).external_subscription_id == "sub_orphan"
    (details,) = await _audit_details(realdb, sub_id)
    assert details["provider_action"] == "adopted"


@pytest.mark.asyncio
async def test_a_held_lease_refuses_and_an_expired_one_is_taken_over(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    fake_stripe.add_card("cus_org")
    held = {"token": "other", "expires_at": (CHANGE_AT + timedelta(minutes=1)).isoformat()}
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org", plan_change_lease=held)

    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth")
    assert exc.value.code == "plan_change_in_progress"
    assert fake_stripe.subscriptions == {}
    assert (await _org_billing(realdb, org_a))[LEASE_KEY] == held  # not ours to drop

    await _change(realdb, org_a, "growth", at=CHANGE_AT + timedelta(minutes=5))
    assert (await _row(realdb, sub_id)).external_subscription_id is not None
    assert LEASE_KEY not in await _org_billing(realdb, org_a)


@pytest.mark.asyncio
async def test_deployed_with_mock_refuses_a_paid_plan_but_not_the_way_down(
    realdb, org_a, monkeypatch, _audit_engine_on_loop
):
    monkeypatch.setattr(settings, "billing_provider", "mock")
    monkeypatch.setattr(settings, "environment", "production")
    free = await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)

    async with realdb.client(key="a", role="admin") as c:
        resp = await _post_change(c, f"{PREFIX}growth")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "billing_not_configured"
    assert (await _row(realdb, sub_id)).plan_id == free
    # Nothing was provisioned for a change that was refused.
    assert "stripe_customer_id" not in await _org_billing(realdb, org_a)

    # An org somehow already on a paid tier can always leave it — at the end
    # of its period, like every move to Free (decisions §483).
    async with realdb.control_sessionmaker()() as s:
        row = (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
        row.plan_id = growth
        await s.commit()
    result = await _change(realdb, org_a, "free")
    assert result.changed is True and result.scheduled is True
    assert (await _row(realdb, sub_id)).pending_plan_id == free


@pytest.mark.asyncio
async def test_an_unregistered_provider_name_counts_as_mock_when_deployed(
    realdb, org_a, monkeypatch
):
    """`stripe` (not `stripe_billing`) falls back to the mock adapter; the
    refusal reads the RESOLVED adapter, so the typo can't hand out paid plans."""
    monkeypatch.setattr(settings, "billing_provider", "stripe")
    monkeypatch.setattr(settings, "environment", "production")
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    await _sub(realdb, org_a, free)
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth")
    assert exc.value.code == "billing_not_configured"


@pytest.mark.asyncio
async def test_local_mock_still_changes_plans(realdb, org_a, monkeypatch, _audit_engine_on_loop):
    """Guard rail 7: with `mock` in local dev a paid plan is one click away."""
    monkeypatch.setattr(settings, "billing_provider", "mock")
    monkeypatch.setattr(settings, "environment", "development")
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    result = await _change(realdb, org_a, "growth")
    assert result.changed is True
    row = await _row(realdb, sub_id)
    assert row.external_subscription_id is None  # nothing live was created
    assert LEASE_KEY not in await _org_billing(realdb, org_a)  # no lease for mock


# ---------------------------------------------------------------------------
# Real-DB: the webhook keeping the row in sync.
# ---------------------------------------------------------------------------


async def _apply(realdb, event: BillingWebhookEvent):
    async with realdb.control_sessionmaker()() as s:
        return await apply_billing_event(s, event=event)


@pytest.mark.asyncio
async def test_invoice_events_move_status_only_along_their_edges(
    realdb, org_a, _audit_engine_on_loop
):
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_wh", status="active")

    def ev(n, kind):
        return BillingWebhookEvent(
            event_id=f"evt_{n}", event_type=kind, external_subscription_id="sub_wh"
        )

    assert (await _apply(realdb, ev(1, "invoice.paid"))).applied is False  # active stays active
    assert (await _apply(realdb, ev(2, "invoice.payment_failed"))).new_status == "past_due"
    assert (await _row(realdb, sub_id)).status == "past_due"
    assert (await _apply(realdb, ev(3, "invoice.paid"))).new_status == "active"
    assert (await _row(realdb, sub_id)).status == "active"

    async with realdb.control_sessionmaker()() as s:
        row = (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
        row.status = "trialing"
        await s.commit()
    # A paid ($0 trial) invoice never ends a trial early; a failure doesn't touch it.
    assert (await _apply(realdb, ev(4, "invoice.paid"))).applied is False
    assert (await _apply(realdb, ev(5, "invoice.payment_failed"))).applied is False
    assert (await _row(realdb, sub_id)).status == "trialing"


@pytest.mark.asyncio
async def test_subscription_events_sync_the_window_and_canceled_is_terminal(
    realdb, org_a, _audit_engine_on_loop
):
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_win", status="active")
    july, aug = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)

    renewal = BillingWebhookEvent(
        event_id="evt_r",
        event_type="customer.subscription.updated",
        external_subscription_id="sub_win",
        status="active",
        current_period_start=july,
        current_period_end=aug,
    )
    result = await _apply(realdb, renewal)
    assert result.applied is True and result.reason == "period_synced"
    row = await _row(realdb, sub_id)
    assert (row.current_period_start, row.current_period_end) == (july, aug)

    # A late event from the PREVIOUS period (Stripe delivers out of order)
    # never moves the window backwards.
    stale = BillingWebhookEvent(
        event_id="evt_stale",
        event_type="customer.subscription.updated",
        external_subscription_id="sub_win",
        status="active",
        current_period_start=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end=july,
    )
    assert (await _apply(realdb, stale)).applied is False
    row = await _row(realdb, sub_id)
    assert (row.current_period_start, row.current_period_end) == (july, aug)

    deleted = BillingWebhookEvent(
        event_id="evt_d",
        event_type="customer.subscription.deleted",
        external_subscription_id="sub_win",
        status="canceled",
    )
    assert (await _apply(realdb, deleted)).new_status == "canceled"
    late = BillingWebhookEvent(
        event_id="evt_late",
        event_type="customer.subscription.updated",
        external_subscription_id="sub_win",
        status="active",
    )
    assert (await _apply(realdb, late)).reason == "subscription_canceled"
    assert (await _row(realdb, sub_id)).status == "canceled"


# ---------------------------------------------------------------------------
# Real-DB: adopting a confirmed card.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_card_endpoint_adopts_the_mock_intent_and_refuses_others(
    realdb, org_a, monkeypatch, _audit_engine_on_loop
):
    monkeypatch.setattr(settings, "billing_provider", "mock")
    await _set_billing(realdb, org_a, stripe_customer_id="mock_cus_a")
    async with realdb.client(key="a", role="admin") as c:
        ok = await c.post(
            "/api/billing/payment-method/default", json={"setup_intent_id": "mock_seti_mock_cus_a"}
        )
        wrong = await c.post(
            "/api/billing/payment-method/default",
            json={"setup_intent_id": "mock_seti_someone_else"},
        )
    assert ok.status_code == 200, ok.text
    assert ok.json()["payment_methods"][0]["is_default"] is True
    assert wrong.status_code == 404
    assert wrong.json()["detail"]["code"] == "setup_intent_not_found"
    async with realdb.sessionmaker("a")() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(
                        AuditLog.action == "billing.payment_method_updated"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert actions  # the adoption is on the trail; the refusal is not a change


@pytest.mark.asyncio
async def test_default_card_endpoint_is_admin_or_cfo_only(realdb, org_a):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/billing/payment-method/default", json={"setup_intent_id": "seti_1"}
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Money-path review: the edges where Stripe and our row can part ways.
# ---------------------------------------------------------------------------


def _live_sub(fake: FakeStripe, sid: str, customer: str = "cus_org", status: str = "active"):
    fake.subscriptions[sid] = {
        "id": sid,
        "status": status,
        "customer": customer,
        "metadata": {},
        "items": [{"id": f"si_{sid}", "price": "price_x"}],
        "period": (
            int(datetime(2026, 6, 1, tzinfo=UTC).timestamp()),
            int(datetime(2026, 7, 1, tzinfo=UTC).timestamp()),
        ),
    }


@pytest.mark.asyncio
async def test_leaving_a_paid_plan_cancels_a_subscription_the_row_lost(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    """A row on a paid plan with no provider id (a create that died before
    recording, or a detach whose cancel failed) still has a Stripe subscription
    charging it; moving to Free must find it by customer and cancel it."""
    await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth)  # no external id
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    _live_sub(fake_stripe, "sub_lost")

    await _change(realdb, org_a, "free")

    assert fake_stripe.subscriptions["sub_lost"]["status"] == "canceled"
    (details,) = await _audit_details(realdb, sub_id)
    assert details["provider_action"] == "canceled"


@pytest.mark.asyncio
async def test_a_failed_cancel_leaves_the_row_paid_and_detached_and_the_retry_heals_it(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    """The provider id is detached BEFORE the cancel (so the cancel's own
    `deleted` webhook can never land on the row). If the cancel then fails, the
    row stays on its paid plan — still what Stripe bills — and the retry finds
    the subscription by customer.

    The immediate cancel is now only for a move that cannot wait for the period
    end (decisions §483) — here a ``past_due`` subscription, which has not paid
    for the period it would be kept in."""
    free = await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_keep", status="past_due")
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    _live_sub(fake_stripe, "sub_keep", status="past_due")

    real_cancel = StripeBillingAdapter.cancel_subscription

    async def failing_cancel(self, external_subscription_id):
        from app.services.billing_adapters.base import BillingProviderError

        raise BillingProviderError("Stripe cancel_subscription failed: HTTP 500")

    monkeypatch.setattr(StripeBillingAdapter, "cancel_subscription", failing_cancel)
    with pytest.raises(Exception):  # noqa: B017 — the provider error propagates as-is
        await _change(realdb, org_a, "free")
    row = await _row(realdb, sub_id)
    assert row.plan_id == growth and row.external_subscription_id is None
    assert fake_stripe.subscriptions["sub_keep"]["status"] == "past_due"  # not canceled
    assert LEASE_KEY not in await _org_billing(realdb, org_a)

    monkeypatch.setattr(StripeBillingAdapter, "cancel_subscription", real_cancel)
    result = await _change(realdb, org_a, "free", at=CHANGE_AT + timedelta(minutes=1))
    assert result.scheduled is False
    assert fake_stripe.subscriptions["sub_keep"]["status"] == "canceled"
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.status == "active"
    assert row.pending_plan_id is None


@pytest.mark.asyncio
async def test_a_mock_override_cannot_repoint_a_row_stripe_still_bills(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_billed")
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org", provider="mock")
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "free")
    assert exc.value.code == "billing_not_configured"
    assert (await _row(realdb, sub_id)).plan_id == growth


@pytest.mark.asyncio
async def test_a_change_that_outlived_its_lease_is_not_recorded(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    """If a slow change's lease runs out and another change takes it over, the
    slow one must not record over the change that now owns the org."""
    import app.services.billing.plan_change as plan_change_module

    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    real_leg = plan_change_module.apply_plan_at_provider

    async def leg_then_taken_over(*args, **kwargs):
        change = await real_leg(*args, **kwargs)
        other = {"token": "someone-else", "expires_at": "2099-01-01T00:00:00+00:00"}
        await _set_billing(realdb, org_a, plan_change_lease=other)
        return change

    monkeypatch.setattr(plan_change_module, "apply_plan_at_provider", leg_then_taken_over)
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth")
    assert exc.value.code == "plan_change_in_progress"
    assert (await _row(realdb, sub_id)).plan_id == free
    # The lease that now owns the org is not ours to drop.
    assert (await _org_billing(realdb, org_a))[LEASE_KEY]["token"] == "someone-else"


@pytest.mark.asyncio
async def test_the_cancels_own_deleted_webhook_cannot_cancel_the_free_row(
    realdb, org_a, _audit_engine_on_loop
):
    """Two connections: a plan change holds the row and detaches the provider id;
    the cancel's `customer.subscription.deleted` arrives meanwhile. It waits on
    the row lock and, after the change commits, no longer matches."""
    import asyncio

    from sqlalchemy import text

    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_echo")
    holder = realdb.control_sessionmaker()()
    try:
        row = (
            await holder.execute(
                select(Subscription).where(Subscription.id == sub_id).with_for_update()
            )
        ).scalar_one()
        row.external_subscription_id = None
        await holder.flush()

        deleted = BillingWebhookEvent(
            event_id="evt_echo",
            event_type="customer.subscription.deleted",
            external_subscription_id="sub_echo",
            status="canceled",
        )
        webhook = asyncio.create_task(_apply(realdb, deleted))

        async def _until_blocked():
            async with realdb.control_sessionmaker()() as probe:
                while True:
                    waiting = (
                        await probe.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                            )
                        )
                    ).scalar_one()
                    if waiting:
                        return
                    await asyncio.sleep(0.02)

        # The real signal: the webhook's SELECT … FOR UPDATE is waiting on us.
        await asyncio.wait_for(_until_blocked(), timeout=10)
        await holder.commit()
        result = await asyncio.wait_for(webhook, timeout=10)
    finally:
        await holder.close()

    assert result.applied is False and result.reason == "unknown_subscription"
    row = await _row(realdb, sub_id)
    assert row.status == "active"


@pytest.mark.asyncio
async def test_dunning_leaves_a_live_row_to_the_provider_until_it_cancels(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    from app.services.billing.dunning_sweep import run_dunning_once

    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_dun", status="past_due")
    _live_sub(fake_stripe, "sub_dun", status="past_due")
    far_future = datetime(2027, 1, 1, tzinfo=UTC)  # well past the grace window

    async with realdb.control_sessionmaker()() as s:
        result = await run_dunning_once(s, now=far_future)
    # Stripe is still retrying: canceling our row would drop the features while
    # Stripe keeps charging for them.
    assert result.canceled == 0
    assert (await _row(realdb, sub_id)).status == "past_due"

    fake_stripe.subscriptions["sub_dun"]["status"] = "canceled"  # webhook lost
    async with realdb.control_sessionmaker()() as s:
        result = await run_dunning_once(s, now=far_future)
    assert result.canceled == 1
    assert (await _row(realdb, sub_id)).status == "canceled"


# ---------------------------------------------------------------------------
# Real-DB: nothing is charged without a confirmed, current preview (§482).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_endpoint_shows_the_providers_figures_and_asks_for_a_card(
    realdb, fake_stripe, org_a
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")  # no card yet

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/billing/change-plan/preview", json={"plan_code": f"{PREFIX}growth"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amount_due"] == "14.80"
    assert body["credit"] == "0.00"
    assert body["next_amount"] == "49.00"
    assert body["currency"] == "USD"
    assert body["payment_method_required"] is True
    assert datetime.fromisoformat(body["next_date"]) == next_month_start(
        datetime.fromtimestamp(body["proration_date"], tz=UTC)
    )
    # A preview charges and creates nothing, and moves no row.
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    assert (await _row(realdb, sub_id)).plan_id == free


@pytest.mark.asyncio
async def test_a_live_change_without_a_confirmed_preview_is_refused(realdb, fake_stripe, org_a):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_a))).scalar_one()
        with pytest.raises(ProviderLegRefused) as exc:
            await change_plan(
                s, org=org, new_plan_code=f"{PREFIX}growth", actor_id=None, change_at=CHANGE_AT
            )
    assert exc.value.code == "plan_change_preview_stale"
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    assert (await _row(realdb, sub_id)).plan_id == free


@pytest.mark.asyncio
async def test_a_quote_the_provider_now_prices_higher_charges_nothing(realdb, fake_stripe, org_a):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    preview = await _preview(realdb, org_a, "growth")
    assert preview.amount_due == Decimal("14.80")

    fake_stripe.preview_due["create"] = 1481  # Stripe would now charge a cent more
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth", quote=_quote_of(preview))
    assert exc.value.code == "plan_change_preview_stale"
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    assert (await _row(realdb, sub_id)).plan_id == free
    assert LEASE_KEY not in await _org_billing(realdb, org_a)  # released


@pytest.mark.asyncio
async def test_a_quote_from_last_month_or_past_its_lifetime_is_refused(realdb, fake_stripe, org_a):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")

    # Priced five minutes before midnight UTC on the 30th; confirmed two minutes
    # after: inside the lifetime, but the anchor (and period) moved on.
    before = datetime(2026, 6, 30, 23, 55, tzinfo=UTC)
    quote = _quote_of(await _preview(realdb, org_a, "growth", at=before))
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(
            realdb, org_a, "growth", at=datetime(2026, 7, 1, 0, 2, tzinfo=UTC), quote=quote
        )
    assert exc.value.code == "plan_change_preview_stale"

    quote = _quote_of(await _preview(realdb, org_a, "growth"))
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth", at=CHANGE_AT + timedelta(minutes=11), quote=quote)
    assert exc.value.code == "plan_change_preview_stale"
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []


@pytest.mark.asyncio
async def test_saving_a_card_never_changes_the_plan(realdb, fake_stripe, org_a):
    """The production report: saving a card used to retry the refused upgrade
    and Stripe charged a prorated amount nobody had confirmed. Adopting a card
    now does exactly that and nothing more."""
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.setup_intents["seti_new"] = {
        "id": "seti_new",
        "customer": "cus_org",
        "status": "succeeded",
        "payment_method": "pm_new",
    }
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/billing/payment-method/default", json={"setup_intent_id": "seti_new"}
        )
    assert resp.status_code == 200, resp.text
    assert fake_stripe.customer("cus_org")["invoice_settings"]["default_payment_method"] == "pm_new"
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    assert [r for r in fake_stripe.requests if r.url.path.startswith("/v1/invoices")] == []
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.external_subscription_id is None
    assert await _audit_details(realdb, sub_id) == []


@pytest.mark.asyncio
async def test_a_confirmation_replays_and_a_fresh_quote_is_a_new_provider_request(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    """The quote's instant is part of the provider Idempotency-Key: the same
    confirmation sent twice is one request at Stripe (its stored answer is
    replayed), and confirming a fresh preview after a decline is a NEW request
    — not the same key with different parameters, which Stripe refuses."""
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    fake_stripe.decline = True

    first = _quote_of(await _preview(realdb, org_a, "growth"))
    for _ in range(2):
        with pytest.raises(BillingPaymentDeclined):
            await _change(realdb, org_a, "growth", quote=first)
    keys = [r.headers["Idempotency-Key"] for r in fake_stripe.calls("POST", "/v1/subscriptions")]
    assert len(keys) == 2 and keys[0] == keys[1]  # the replayed decline
    assert str(int(first.proration_date.timestamp())) in keys[0]

    fake_stripe.decline = False
    later = CHANGE_AT + timedelta(minutes=1)
    fresh = _quote_of(await _preview(realdb, org_a, "growth", at=later))
    result = await _change(realdb, org_a, "growth", at=later, quote=fresh)
    assert result.changed is True
    creates = fake_stripe.calls("POST", "/v1/subscriptions")
    assert creates[-1].headers["Idempotency-Key"] != keys[0]
    assert len(fake_stripe.subscriptions) == 1  # charged once

    # Confirming the same quote again is the already-on-plan no-op: no provider call.
    again = await _change(realdb, org_a, "growth", at=later, quote=fresh)
    assert again.changed is False
    assert len(fake_stripe.calls("POST", "/v1/subscriptions")) == len(creates)
    assert (await _row(realdb, sub_id)).external_subscription_id in fake_stripe.subscriptions


# ---------------------------------------------------------------------------
# A move to Free waits for the end of the paid period (decisions §483).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_and_resume_send_only_the_scheduled_end_without_proration(fake_stripe):
    _live_sub(fake_stripe, "sub_s")
    adapter = _adapter(fake_stripe)

    scheduled = await adapter.cancel_subscription_at_period_end("sub_s", idempotency_key="ks")
    resumed = await adapter.resume_subscription("sub_s", idempotency_key="kr")

    schedule, resume = fake_stripe.calls("POST", "/v1/subscriptions/sub_s")
    # The exact forms: the scheduled end and an explicit "no proration" —
    # nothing is credited or refunded, and no item changes.
    assert FakeStripe.form(schedule) == {
        "cancel_at_period_end": "true",
        "proration_behavior": "none",
    }
    assert FakeStripe.form(resume) == {
        "cancel_at_period_end": "false",
        "proration_behavior": "none",
    }
    assert schedule.headers["Idempotency-Key"] == "ks"
    assert resume.headers["Idempotency-Key"] == "kr"
    assert schedule.headers["Stripe-Version"] == "2026-11-18.endive"
    assert fake_stripe.calls("DELETE", "/v1/subscriptions/sub_s") == []
    assert scheduled.cancel_at_period_end is True
    assert scheduled.status == "active"
    assert scheduled.current_period_end == datetime(2026, 7, 1, tzinfo=UTC)
    assert resumed.cancel_at_period_end is False


@pytest.mark.asyncio
async def test_a_paid_change_while_an_end_is_scheduled_also_clears_it(fake_stripe):
    _live_sub(fake_stripe, "sub_r")
    fake_stripe.subscriptions["sub_r"]["cancel_at_period_end"] = True
    adapter = _adapter(fake_stripe)

    await adapter.preview_plan_change(_preview_request(external_subscription_id="sub_r"))
    first, recurring = fake_stripe.calls("POST", "/v1/invoices/create_preview")
    assert FakeStripe.form(first)["subscription_details[cancel_at_period_end]"] == "false"
    assert FakeStripe.form(recurring)["subscription_details[cancel_at_period_end]"] == "false"

    await adapter.change_subscription_plan(
        "sub_r",
        price_id="price_scale",
        overage_price_id=None,
        plan_code="scale",
        idempotency_key="k",
    )
    (update,) = fake_stripe.calls("POST", "/v1/subscriptions/sub_r")
    assert FakeStripe.form(update)["cancel_at_period_end"] == "false"
    assert fake_stripe.subscriptions["sub_r"]["cancel_at_period_end"] is False


async def _growth_billed_by_stripe(realdb, fake_stripe, org_id, sid="sub_live", *, month=None):
    """A Growth row billed by a live Stripe subscription — in June 2026 (the
    service tests' clock, `CHANGE_AT`), or in ``month`` for a test that goes
    through the HTTP routes, which run on the real clock."""
    free = await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    await _plan(realdb, "scale", "199.00")
    sub_id = await _sub(realdb, org_id, growth, external_id=sid)
    await _set_billing(realdb, org_id, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    _live_sub(fake_stripe, sid)
    if month is not None:
        start, end = month
        fake_stripe.subscriptions[sid]["period"] = (int(start.timestamp()), int(end.timestamp()))
        async with realdb.control_sessionmaker()() as s:
            row = (
                await s.execute(select(Subscription).where(Subscription.id == sub_id))
            ).scalar_one()
            row.current_period_start, row.current_period_end = start, end
            await s.commit()
    return free, growth, sub_id


def _this_month() -> tuple[datetime, datetime]:
    """The real UTC calendar month — the window an HTTP-route test's live
    subscription is in, since the routes read the real clock."""
    from app.services.billing.provider_sync import calendar_month

    return calendar_month(datetime.now(UTC))


@pytest.mark.asyncio
async def test_the_preview_of_a_move_to_free_keeps_the_plan_until_the_period_ends(
    realdb, fake_stripe, org_a
):
    month = _this_month()
    await _growth_billed_by_stripe(realdb, fake_stripe, org_a, month=month)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/billing/change-plan/preview", json={"plan_code": f"{PREFIX}free"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Nothing charged, nothing credited, nothing recurring — and the date the
    # move takes effect, from the subscription's own period end.
    assert body["amount_due"] == "0.00"
    assert body["credit"] == "0.00"
    assert body["next_amount"] is None and body["next_date"] is None
    assert body["effective_at"] == month[1].isoformat().replace("+00:00", "Z")
    assert body["clears_scheduled_change"] is False
    assert body["currency"] == "USD"
    # No cancel was priced: there is no credit to ask Stripe about.
    assert fake_stripe.calls("POST", "/v1/invoices/create_preview") == []


@pytest.mark.asyncio
async def test_the_end_of_period_webhook_moves_the_org_to_free_once(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a)
    await _change(realdb, org_a, "free")

    # Mid-period nothing moves: the subscription is live and paid for.
    row = await _row(realdb, sub_id)
    assert row.plan_id == growth and row.pending_plan_id == free

    deleted = BillingWebhookEvent(
        event_id="evt_period_end",
        event_type="customer.subscription.deleted",
        external_subscription_id="sub_live",
        status="canceled",
    )
    result = await _apply(realdb, deleted)
    assert result.applied is True and result.reason == "scheduled_plan_applied"
    row = await _row(realdb, sub_id)
    # The shape the immediate cancel always left: Free, live, no provider id.
    assert row.plan_id == free and row.status == "active"
    assert row.external_subscription_id is None
    assert row.pending_plan_id is None and row.pending_plan_effective_at is None
    assert row.current_period_start == datetime(2026, 7, 1, tzinfo=UTC)
    assert row.current_period_end == datetime(2026, 8, 1, tzinfo=UTC)
    (applied,) = await _audit_details(realdb, sub_id)
    assert applied["scheduled"] is True
    assert applied["from_plan"] == f"{PREFIX}growth" and applied["to_plan"] == f"{PREFIX}free"
    assert applied["event_id"] == "evt_period_end"
    assert applied["provider_action"] == "ended_at_period_end"
    assert applied["ended_external_subscription_id"] == "sub_live"

    # A redelivery finds no row by that provider id: nothing happens twice.
    again = await _apply(realdb, deleted)
    assert again.applied is False and again.reason == "unknown_subscription"
    assert len(await _audit_details(realdb, sub_id)) == 1


@pytest.mark.asyncio
async def test_a_retried_move_to_free_is_a_no_op(realdb, fake_stripe, org_a, _audit_engine_on_loop):
    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a)
    await _change(realdb, org_a, "free")
    again = await _change(realdb, org_a, "free", at=CHANGE_AT + timedelta(minutes=2))
    assert again.changed is False and again.reason == "already_scheduled"
    assert again.effective_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert len(fake_stripe.calls("POST", "/v1/subscriptions/sub_live")) == 1
    assert len(await _audit_details(realdb, sub_id, action="billing.plan_change_scheduled")) == 1


@pytest.mark.asyncio
async def test_keep_clears_the_scheduled_end_at_stripe_and_on_the_row(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    month = _this_month()
    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a, month=month)
    async with realdb.client(key="a", role="admin") as c:
        scheduled = await _post_change(c, f"{PREFIX}free")
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()["scheduled"] is True
    assert scheduled.json()["effective_at"] == month[1].isoformat().replace("+00:00", "Z")
    assert fake_stripe.subscriptions["sub_live"]["cancel_at_period_end"] is True

    # Keep is admin / cfo only, like every plan change.
    async with realdb.client(key="a", role="ap_clerk") as c:
        clerk = await c.post("/api/billing/scheduled-change/cancel")
    assert clerk.status_code == 403
    async with realdb.client(key="a", role="ap_manager") as c:
        manager = await c.post("/api/billing/scheduled-change/cancel")
    assert manager.status_code == 403

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/billing/scheduled-change/cancel")
        summary = await c.get("/api/billing/subscription")
        again = await c.post("/api/billing/scheduled-change/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cleared": True, "plan_code": f"{PREFIX}growth"}
    # Schedule, then resume — the idempotent second Keep made no call.
    schedule, resume = fake_stripe.calls("POST", "/v1/subscriptions/sub_live")
    assert FakeStripe.form(schedule)["cancel_at_period_end"] == "true"
    assert FakeStripe.form(resume) == {
        "cancel_at_period_end": "false",
        "proration_behavior": "none",
    }
    assert fake_stripe.subscriptions["sub_live"]["cancel_at_period_end"] is False
    row = await _row(realdb, sub_id)
    assert row.plan_id == growth and row.pending_plan_id is None
    assert row.pending_plan_effective_at is None and row.external_subscription_id == "sub_live"
    assert summary.json()["subscription"]["scheduled_change"] is None
    # Idempotent: nothing pending is a clean no-op, no second provider call.
    assert again.status_code == 200 and again.json()["cleared"] is False
    (kept,) = await _audit_details(realdb, sub_id, action="billing.scheduled_change_canceled")
    assert kept["canceled_to_plan"] == f"{PREFIX}free"
    assert kept["provider_action"] == "resumed"
    assert kept["reason"] == "kept_by_customer"
    assert LEASE_KEY not in await _org_billing(realdb, org_a)


@pytest.mark.asyncio
async def test_the_summary_shows_the_pending_move_and_keep_refuses_once_it_is_due(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    from app.services.billing.scheduled_downgrade import keep_current_plan

    month = _this_month()
    await _growth_billed_by_stripe(realdb, fake_stripe, org_a, month=month)
    async with realdb.client(key="a", role="admin") as c:
        await _post_change(c, f"{PREFIX}free")
        summary = (await c.get("/api/billing/subscription")).json()
    assert summary["plan"]["code"] == f"{PREFIX}growth"
    assert summary["subscription"]["status"] == "active"
    assert summary["subscription"]["scheduled_change"] == {
        "plan_code": f"{PREFIX}free",
        "plan_name": "Free",
        "effective_at": month[1].isoformat().replace("+00:00", "Z"),
    }

    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_a))).scalar_one()
        with pytest.raises(ProviderLegRefused) as exc:
            await keep_current_plan(s, org=org, actor_id=None, now=month[1] + timedelta(minutes=5))
    assert exc.value.code == "scheduled_change_already_due"
    assert fake_stripe.subscriptions["sub_live"]["cancel_at_period_end"] is True


@pytest.mark.asyncio
async def test_choosing_a_paid_plan_while_a_move_is_pending_takes_it_back(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a)
    await _change(realdb, org_a, "free")

    preview = await _preview(realdb, org_a, "scale", at=CHANGE_AT + timedelta(minutes=1))
    assert preview.clears_scheduled_change is True and preview.effective_at is None
    swap_preview = fake_stripe.calls("POST", "/v1/invoices/create_preview")[0]
    assert FakeStripe.form(swap_preview)["subscription_details[cancel_at_period_end]"] == "false"

    up = await _change(realdb, org_a, "scale", at=CHANGE_AT + timedelta(minutes=1))
    assert up.changed is True and up.scheduled is False
    update = fake_stripe.calls("POST", "/v1/subscriptions/sub_live")[-1]
    form = FakeStripe.form(update)
    # One request: the swap, prorated as before, and the scheduled end cleared.
    assert form["cancel_at_period_end"] == "false"
    assert form["proration_behavior"] == "always_invoice"
    assert fake_stripe.subscriptions["sub_live"]["cancel_at_period_end"] is False
    row = await _row(realdb, sub_id)
    assert row.pending_plan_id is None and row.pending_plan_effective_at is None
    details = (await _audit_details(realdb, sub_id))[-1]
    assert details["cleared_pending_plan_id"] == str(free)


@pytest.mark.asyncio
async def test_the_sweep_applies_a_move_whose_end_webhook_was_lost(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    from app.services.billing.dunning_sweep import run_dunning_once

    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a)
    await _change(realdb, org_a, "free")
    after = datetime(2026, 7, 1, 2, 0, tzinfo=UTC)

    # Before the date: untouched.
    async with realdb.control_sessionmaker()() as s:
        early = await run_dunning_once(s, now=datetime(2026, 6, 30, tzinfo=UTC))
    assert early.downgrades_applied == 0
    # Past the date but Stripe has not ended it yet (still set to end): wait.
    async with realdb.control_sessionmaker()() as s:
        waiting = await run_dunning_once(s, now=after)
    assert waiting.downgrades_applied == 0 and waiting.downgrades_dropped == 0
    assert (await _row(realdb, sub_id)).plan_id == growth

    fake_stripe.subscriptions["sub_live"]["status"] = "canceled"  # ended; webhook lost
    async with realdb.control_sessionmaker()() as s:
        applied = await run_dunning_once(s, now=after)
    assert applied.downgrades_applied == 1 and applied.downgrade_failures == 0
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.status == "active"
    assert row.external_subscription_id is None and row.pending_plan_id is None
    (details,) = await _audit_details(realdb, sub_id)
    assert details["scheduled"] is True
    assert details["reason"] == "provider_ended_webhook_missed"

    # Idempotent: the next tick finds nothing due.
    async with realdb.control_sessionmaker()() as s:
        again = await run_dunning_once(s, now=after)
    assert again.downgrades_applied == 0


@pytest.mark.asyncio
async def test_the_sweep_drops_a_pending_move_the_provider_renewed(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    from app.services.billing.dunning_sweep import run_dunning_once

    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a)
    await _change(realdb, org_a, "free")
    # Resumed in Stripe's dashboard (or a Keep whose write was lost): it renews.
    fake_stripe.subscriptions["sub_live"]["cancel_at_period_end"] = False
    async with realdb.control_sessionmaker()() as s:
        result = await run_dunning_once(s, now=datetime(2026, 7, 1, 2, 0, tzinfo=UTC))
    assert result.downgrades_dropped == 1 and result.downgrades_applied == 0
    row = await _row(realdb, sub_id)
    # Still billed for Growth, so still on Growth.
    assert row.plan_id == growth and row.pending_plan_id is None
    (dropped,) = await _audit_details(realdb, sub_id, action="billing.scheduled_change_canceled")
    assert dropped["reason"] == "provider_renewed"


@pytest.mark.asyncio
async def test_with_mock_the_move_waits_and_the_sweep_makes_it(
    realdb, org_a, monkeypatch, _audit_engine_on_loop
):
    """Local dev / CI: no provider, the row is the whole state; the sweep moves
    the org once the period it keeps is over."""
    from app.services.billing.dunning_sweep import run_dunning_once

    monkeypatch.setattr(settings, "billing_provider", "mock")
    monkeypatch.setattr(settings, "environment", "development")
    free = await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth)

    preview = await _preview(realdb, org_a, "free")
    assert preview.effective_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert preview.credit == Decimal("0.00") and preview.amount_due == Decimal("0.00")
    result = await _change(realdb, org_a, "free")
    assert result.scheduled is True and result.effective_at == datetime(2026, 7, 1, tzinfo=UTC)
    row = await _row(realdb, sub_id)
    assert row.plan_id == growth and row.pending_plan_id == free
    assert LEASE_KEY not in await _org_billing(realdb, org_a)  # no lease for mock

    async with realdb.control_sessionmaker()() as s:
        swept = await run_dunning_once(s, now=datetime(2026, 7, 1, 0, 1, tzinfo=UTC))
    assert swept.downgrades_applied == 1
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.pending_plan_id is None and row.status == "active"
    (details,) = await _audit_details(realdb, sub_id)
    assert details["reason"] == "period_ended"


@pytest.mark.asyncio
async def test_a_past_due_subscription_still_moves_to_free_at_once(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    """It has not paid for the period it would be kept in (decisions §483)."""
    free = await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_pd", status="past_due")
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    _live_sub(fake_stripe, "sub_pd", status="past_due")

    preview = await _preview(realdb, org_a, "free")
    assert preview.effective_at is None
    result = await _change(realdb, org_a, "free")
    assert result.scheduled is False
    assert fake_stripe.subscriptions["sub_pd"]["status"] == "canceled"
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.pending_plan_id is None


@pytest.mark.asyncio
async def test_keep_racing_the_end_of_period_webhook_is_refused_cleanly(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    """The webhook makes the move between Keep's peek and its locked write:
    Keep answers ``scheduled_change_already_due`` (a coded 409), not a crash
    reading rows its own rollback expired."""
    from app.services.billing.scheduled_downgrade import keep_current_plan

    month = _this_month()
    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a, month=month)
    async with realdb.client(key="a", role="admin") as c:
        await _post_change(c, f"{PREFIX}free")

    real_resume = StripeBillingAdapter.resume_subscription

    async def resume_then_webhook(self, external_subscription_id, *, idempotency_key):
        answer = await real_resume(self, external_subscription_id, idempotency_key=idempotency_key)
        await _apply(
            realdb,
            BillingWebhookEvent(
                event_id="evt_race",
                event_type="customer.subscription.deleted",
                external_subscription_id=external_subscription_id,
                status="canceled",
            ),
        )
        return answer

    monkeypatch.setattr(StripeBillingAdapter, "resume_subscription", resume_then_webhook)
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_a))).scalar_one()
        with pytest.raises(ProviderLegRefused) as exc:
            await keep_current_plan(s, org=org, actor_id=None)
    assert exc.value.code == "scheduled_change_already_due"
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.pending_plan_id is None
    assert await _audit_details(realdb, sub_id, action="billing.scheduled_change_canceled") == []
    assert LEASE_KEY not in await _org_billing(realdb, org_a)


@pytest.mark.asyncio
async def test_a_paid_change_takes_back_an_end_stripe_has_and_our_row_lost(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    """A schedule reached Stripe but its record failed (or raced this change):
    Stripe will end the subscription, our row shows nothing pending. The next
    paid change must still tell Stripe to keep renewing, or the org — having
    just paid for the upgrade — would be left on no subscription at the
    period end."""
    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a)
    fake_stripe.subscriptions["sub_live"]["cancel_at_period_end"] = True
    assert (await _row(realdb, sub_id)).pending_plan_id is None

    await _change(realdb, org_a, "scale")

    (update,) = fake_stripe.calls("POST", "/v1/subscriptions/sub_live")
    assert FakeStripe.form(update)["cancel_at_period_end"] == "false"
    assert fake_stripe.subscriptions["sub_live"]["cancel_at_period_end"] is False


@pytest.mark.asyncio
async def test_a_schedule_whose_row_moved_before_the_lease_tells_stripe_nothing(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    """Another change commits between the schedule's peek and its lease: the
    re-read under the lease refuses before any Stripe call, so Stripe is never
    told to end a subscription our row says continues."""
    import app.services.billing.scheduled_downgrade as sd

    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a)
    async with realdb.control_sessionmaker()() as s:
        scale_id = (
            await s.execute(select(Plan.id).where(Plan.code == f"{PREFIX}scale"))
        ).scalar_one()
    real_claim = sd.claim_plan_change_lease

    async def claim_after_a_racer(control_db, org, *, now):
        async with realdb.control_sessionmaker()() as s:
            row = (
                await s.execute(select(Subscription).where(Subscription.id == sub_id))
            ).scalar_one()
            row.plan_id = scale_id  # the racing upgrade committed first
            await s.commit()
        return await real_claim(control_db, org, now=now)

    monkeypatch.setattr(sd, "claim_plan_change_lease", claim_after_a_racer)
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "free")
    assert exc.value.code == "plan_change_in_progress"
    assert fake_stripe.calls("POST", "/v1/subscriptions/sub_live") == []
    row = await _row(realdb, sub_id)
    assert row.plan_id == scale_id and row.pending_plan_id is None
    assert LEASE_KEY not in await _org_billing(realdb, org_a)


@pytest.mark.asyncio
async def test_a_double_submitted_schedule_is_recorded_once(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    """The second of two submits peeked before the first recorded the move:
    under the lease it finds the move already scheduled and answers the no-op —
    no second Stripe call, no second audit row."""
    import app.services.billing.scheduled_downgrade as sd

    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a)
    first = await _change(realdb, org_a, "free")
    # Replay the second submit's stale peek: clear the pending state on the
    # session it peeks with, then let the lease re-read find the real row.
    async with realdb.control_sessionmaker()() as s:
        row = (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
        org = (await s.execute(select(Organization).where(Organization.id == org_a))).scalar_one()
        growth_plan = (await s.execute(select(Plan).where(Plan.id == growth))).scalar_one()
        free_plan = (await s.execute(select(Plan).where(Plan.id == free))).scalar_one()
        # What the stale peek saw — on a detached copy, so nothing flushes it.
        s.expunge(row)
        row.pending_plan_id = None
        again = await sd.schedule_move_to_free(
            s,
            org=org,
            adapter=sd.billing_adapter_for(org),
            subscription=row,
            current_plan=growth_plan,
            new_plan=free_plan,
            actor_id=None,
            now=CHANGE_AT,
            quote_proration_date=None,
        )
    assert first.scheduled is True
    assert again.changed is False and again.reason == "already_scheduled"
    assert again.effective_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert len(fake_stripe.calls("POST", "/v1/subscriptions/sub_live")) == 1
    assert len(await _audit_details(realdb, sub_id, action="billing.plan_change_scheduled")) == 1


@pytest.mark.asyncio
async def test_keep_after_a_paid_change_took_the_move_back_is_a_no_op(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    """A paid change clears the pending move between Keep's peek and its
    lease: Keep answers ``cleared: false`` — not "already taken effect", which
    would be false — and asks Stripe nothing."""
    import app.services.billing.scheduled_downgrade as sd

    month = _this_month()
    free, growth, sub_id = await _growth_billed_by_stripe(realdb, fake_stripe, org_a, month=month)
    async with realdb.client(key="a", role="admin") as c:
        await _post_change(c, f"{PREFIX}free")
    async with realdb.control_sessionmaker()() as s:
        scale_id = (
            await s.execute(select(Plan.id).where(Plan.code == f"{PREFIX}scale"))
        ).scalar_one()
    real_claim = sd.claim_plan_change_lease

    async def claim_after_a_paid_change(control_db, org, *, now):
        async with realdb.control_sessionmaker()() as s:
            row = (
                await s.execute(select(Subscription).where(Subscription.id == sub_id))
            ).scalar_one()
            row.plan_id = scale_id
            row.pending_plan_id = None
            row.pending_plan_effective_at = None
            await s.commit()
        return await real_claim(control_db, org, now=now)

    monkeypatch.setattr(sd, "claim_plan_change_lease", claim_after_a_paid_change)
    calls_before = len(fake_stripe.calls("POST", "/v1/subscriptions/sub_live"))
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_a))).scalar_one()
        result = await sd.keep_current_plan(s, org=org, actor_id=None)
    assert result.cleared is False and result.plan_code == f"{PREFIX}scale"
    assert len(fake_stripe.calls("POST", "/v1/subscriptions/sub_live")) == calls_before
    assert LEASE_KEY not in await _org_billing(realdb, org_a)
