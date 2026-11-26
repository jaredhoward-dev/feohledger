"""Grant or revoke a tenant's partner (reseller) capability — operator only.

Thin wrapper around ``app.services.partner_capability.set_partner_capability``.
``Organization.is_partner`` is what every ``/api/partner`` route requires and
what shows the Partner Admin page (``docs/decisions.md`` §574). Nothing in the
app sets it: becoming a reseller is an agreement with the operator, so this
script is the way. Each change is audited into the tenant's own trail
(``organization.partner_capability_granted`` / ``_revoked``) before it is made.

Usage (from ``backend/``, against the deployment's control-plane database):

    python scripts/set_partner_capability.py --slug acme --grant --reason "CTR-42 signed"
    python scripts/set_partner_capability.py --slug acme --revoke --reason "CTR-42 ended"

Exit codes: 0 changed (or already so), 1 no such tenant, 2 the audit row could
not be written (nothing changed), 3 revoke refused — the tenant still parents
child tenants (its admin detaches them on Partner Admin first).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.database import control_engine, control_session_factory
from app.services.partner_capability import (
    AuditWriteFailed,
    PartnerHasChildren,
    TenantNotFound,
    set_partner_capability,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grant or revoke one tenant's partner (reseller) capability, audited."
    )
    parser.add_argument("--slug", required=True, help="Tenant slug, e.g. 'acme'")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--grant", action="store_true", help="Make the tenant a partner")
    mode.add_argument("--revoke", action="store_true", help="Take the capability away")
    parser.add_argument(
        "--reason",
        default=None,
        help="Contract or ticket reference, recorded in the audit row. No personal data.",
    )
    return parser


async def run(slug: str, *, enabled: bool, reason: str | None) -> int:
    async with control_session_factory() as session:
        try:
            result = await set_partner_capability(session, slug, enabled=enabled, reason=reason)
        except TenantNotFound:
            print(f"No organization with slug {slug!r}.", file=sys.stderr)
            return 1
        except AuditWriteFailed as exc:
            print(
                f"The audit row could not be written, so nothing changed ({exc}).",
                file=sys.stderr,
            )
            return 2
        except PartnerHasChildren as exc:
            print(
                f"Tenant {slug!r} still parents {exc.count} child tenant(s). Its admin "
                "detaches them on Partner Admin first; nothing changed.",
                file=sys.stderr,
            )
            return 3

    state = "a partner" if result.is_partner else "not a partner"
    if not result.changed:
        print(f"Tenant {slug!r} is already {state}. Nothing changed.")
    else:
        print(f"Tenant {slug!r} is now {state}; audited.")
        print("  Its admins see the change on their next page load (/api/auth/me).")
    return 0


async def _main() -> int:
    args = build_parser().parse_args()
    try:
        return await run(args.slug, enabled=bool(args.grant), reason=args.reason)
    finally:
        await control_engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
