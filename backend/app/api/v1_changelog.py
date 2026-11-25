"""Dated change notes for the public developer contract (``/api/v1`` + webhooks).

One entry per change an integrator has to hear about, newest first. The list is
the single source: ``api/v1_openapi`` publishes it as ``info.x-changelog`` in
``/api/v1/openapi.json`` and renders it under **Changes** on ``/api/v1/docs``,
and ``backend/docs/public-api.md`` § Changes carries the same entries in prose;
``tests/test_public_api_changelog.py`` fails when the three disagree.

``info.version`` is not bumped for an entry: it tracks the path prefix
(``v1``), and a new major version is a new prefix (`public-api.md` §
Versioning & deprecation policy). The changelog is how a change *within*
``v1`` is announced.

``affects`` names the event types whose subscribers are affected and
``subscribe_to`` the event types they should add. Nothing changes a customer's
subscription on their behalf (decisions §548): ``scripts/public_api_change_report.py``
lists, read-only, the subscriptions an entry affects that do not yet carry its
``subscribe_to`` types, so the operator can tell those organisations directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.models.webhook import (
    EVENT_INVOICE_APPROVED,
    EVENT_INVOICE_ERP_BILL_VOIDED,
    EVENT_PAYMENT_VOIDED,
)


@dataclass(frozen=True)
class PublicApiChange:
    id: str
    on: date
    title: str
    detail: str
    affects: tuple[str, ...] = ()
    subscribe_to: tuple[str, ...] = ()


#: Newest first.
PUBLIC_API_CHANGES: tuple[PublicApiChange, ...] = (
    PublicApiChange(
        id="2026-11-25-void-events",
        on=date(2026, 11, 25),
        title="A voided payment sends payment.voided, no longer invoice.approved",
        detail=(
            "When a payment is voided (POST /api/payments/{id}/void) the invoice returns to "
            "approved without anyone approving it. That return used to send a second "
            "invoice.approved; it now sends payment.voided instead, with the same invoice "
            "fields, and invoice.approved is sent only for an approval. If your receiver "
            "re-queued a bill or undid a settlement on the invoice.approved that followed a "
            "void, subscribe to payment.voided and do it there. New in the same release: "
            "invoice.erp_bill_voided, sent when an invoice's bill is voided in the ERP from "
            "a duplicate or fraud flag; it is not an approval, so never re-post the bill on "
            "it. Existing subscriptions are not changed for you."
        ),
        affects=(EVENT_INVOICE_APPROVED,),
        subscribe_to=(EVENT_PAYMENT_VOIDED, EVENT_INVOICE_ERP_BILL_VOIDED),
    ),
)


def changelog_spec() -> list[dict[str, Any]]:
    """The entries as the JSON the published spec carries (``info.x-changelog``)."""
    return [
        {
            "id": change.id,
            "date": change.on.isoformat(),
            "title": change.title,
            "detail": change.detail,
            "affects": list(change.affects),
            "subscribe_to": list(change.subscribe_to),
        }
        for change in PUBLIC_API_CHANGES
    ]


def missing_subscriptions(change: PublicApiChange, event_types: list[str] | None) -> list[str]:
    """The ``subscribe_to`` types a subscription to ``event_types`` lacks, or
    ``[]`` when the change does not affect it (it subscribes to none of
    ``affects``) or it already carries them all."""
    subscribed = set(event_types or [])
    if not subscribed & set(change.affects):
        return []
    return [event for event in change.subscribe_to if event not in subscribed]
