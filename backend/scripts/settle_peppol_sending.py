"""Settle a PEPPOL transmission stuck in ``sending`` — operator only.

Thin wrapper around ``app.services.peppol_send.settle_stale_sending``
(``docs/decisions.md`` §580). ``POST /api/invoices/{id}/peppol-send`` commits a
``sending`` reservation before it calls the Access Point and records the
outcome in a second commit (§578). A process that dies between the two leaves
the row ``sending`` for good: every resend answers ``already_sent`` and the
approval recall is refused. This script asks the Access Point what became of
the document, by the row's business message id, and records its answer —
``sent`` / ``delivered`` with the AP's MessageId, or ``failed`` when the AP
reports a failure or holds no such transmission (then the invoice can be sent
again). It never marks a row failed without that answer. The change is
audited as ``invoice.peppol_send_settled`` in the same transaction, before the
row changes. The procedure is ``backend/docs/peppol.md`` § Send route.

Usage (from ``backend/``, against the deployment's control-plane database):

    python scripts/settle_peppol_sending.py --slug acme --invoice <uuid> --reason "TICKET-123"

Exit codes: 0 settled, 1 no such tenant / invoice has no ``sending`` row,
2 refused: the row is younger than the stale threshold (it may still be in
flight), 3 refused: the Access Point could not be asked or did not answer
(nothing changed; retry later or ask the AP's support).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import _make_tenant_url, control_engine, control_session_factory
from app.db_engine import create_async_engine
from app.models.organization import Organization
from app.services.peppol_send import STALE_SENDING_AFTER, SettleRefused, settle_stale_sending

_EXIT_FOR = {
    "no_sending_transmission": 1,
    "sending_too_recent": 2,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Settle one invoice's stuck 'sending' PEPPOL transmission from the "
        "Access Point's own record, audited."
    )
    parser.add_argument("--slug", required=True, help="Tenant slug, e.g. 'acme'")
    parser.add_argument("--invoice", required=True, type=uuid.UUID, help="Invoice id")
    parser.add_argument(
        "--reason",
        default=None,
        help="Ticket reference or one sentence, recorded in the audit row. No personal data.",
    )
    return parser


async def run(slug: str, invoice_id: uuid.UUID, reason: str | None) -> int:
    async with control_session_factory() as ctrl:
        org = (
            await ctrl.execute(select(Organization).where(Organization.slug == slug))
        ).scalar_one_or_none()
    if org is None:
        print(f"No organization with slug {slug!r}.", file=sys.stderr)
        return 1
    peppol_config = (org.settings or {}).get("peppol") or {}

    engine = create_async_engine(_make_tenant_url(org.db_name), pool_size=1, max_overflow=0)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            try:
                result = await settle_stale_sending(
                    db, invoice_id=invoice_id, peppol_config=peppol_config, reason=reason
                )
            except SettleRefused as exc:
                if exc.code == "sending_too_recent":
                    minutes = int(STALE_SENDING_AFTER.total_seconds() // 60)
                    print(
                        f"The transmission went to 'sending' less than {minutes} minutes ago "
                        "and may still be in flight. Nothing changed; run again later.",
                        file=sys.stderr,
                    )
                else:
                    print(f"Refused ({exc}). Nothing changed.", file=sys.stderr)
                return _EXIT_FOR.get(exc.code, 3)
    finally:
        await engine.dispose()

    print(
        f"Transmission {result.transmission_id} is now {result.status!r} "
        f"(Access Point answered {result.lookup_outcome!r}); audited as "
        "invoice.peppol_send_settled."
    )
    if result.status == "failed":
        print("  The invoice can be sent over PEPPOL again, and its approval recalled.")
    return 0


async def _main() -> int:
    args = build_parser().parse_args()
    try:
        return await run(args.slug, args.invoice, args.reason)
    finally:
        await control_engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
