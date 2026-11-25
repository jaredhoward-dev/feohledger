"""Which webhook subscriptions a public-API change note affects. Read-only.

`app/api/v1_changelog.PUBLIC_API_CHANGES` is the dated list of changes an
integrator has to hear about (`backend/docs/public-api.md` § Changes,
`docs/decisions.md` §548). Each entry names the event types whose subscribers
are affected (``affects``) and the types they should add (``subscribe_to``).
This lists the active subscriptions that subscribe to one of ``affects`` but
lack a ``subscribe_to`` type, so the operator can tell those organisations
directly:

    python scripts/public_api_change_report.py                 # the newest entry
    python scripts/public_api_change_report.py --change <id>   # a named entry
    python scripts/public_api_change_report.py --list          # every entry's id

It changes nothing and sends nothing: no subscription is edited for a customer
(their receiver may not handle a type it never asked for), and there is no
email path for API change notes. It prints organisation slugs and subscription
ids only; no URL, name or contact.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.api.v1_changelog import (  # noqa: E402
    PUBLIC_API_CHANGES,
    PublicApiChange,
    missing_subscriptions,
)

#: `--change` named no entry.
EXIT_UNKNOWN_CHANGE = 2


def find_change(change_id: str | None) -> PublicApiChange | None:
    if change_id is None:
        return PUBLIC_API_CHANGES[0] if PUBLIC_API_CHANGES else None
    return next((c for c in PUBLIC_API_CHANGES if c.id == change_id), None)


async def _report(change: PublicApiChange) -> int:
    from app.database import control_session_factory
    from app.models.organization import Organization
    from app.models.webhook import WebhookSubscription

    async with control_session_factory() as db:
        rows = (
            await db.execute(
                select(WebhookSubscription.id, WebhookSubscription.event_types, Organization.slug)
                .join(Organization, Organization.id == WebhookSubscription.organization_id)
                .where(WebhookSubscription.active.is_(True))
                .order_by(Organization.slug, WebhookSubscription.created_at)
            )
        ).all()
    affected = [
        (slug, sub_id, missing)
        for sub_id, event_types, slug in rows
        if (missing := missing_subscriptions(change, event_types))
    ]
    print(f"{change.id} ({change.on.isoformat()}): {change.title}")
    if not affected:
        print("No active subscription needs telling.")
        return 0
    for slug, sub_id, missing in affected:
        print(f"{slug}  subscription {sub_id}  lacks {', '.join(missing)}")
    orgs = len({slug for slug, _, _ in affected})
    print(f"{len(affected)} subscription(s) in {orgs} organisation(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--change", help="a change-note id (default: the newest)")
    parser.add_argument("--list", action="store_true", help="list the change-note ids")
    args = parser.parse_args(argv)
    if args.list:
        for change in PUBLIC_API_CHANGES:
            print(f"{change.id}  {change.on.isoformat()}  {change.title}")
        return 0
    change = find_change(args.change)
    if change is None:
        print(f"No change note {args.change!r}; --list shows them.", file=sys.stderr)
        return EXIT_UNKNOWN_CHANGE
    return asyncio.run(_report(change))


if __name__ == "__main__":
    sys.exit(main())
