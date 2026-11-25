"""Charge a mock virtual card locally, as its issuer's webhook would.

A payment run's card leg is paid only once its card's charge SETTLES
(decisions §537, §544), and that comes from the card provider's webhook. On a laptop the
provider is `mock`, so this sends that webhook instead: signed, through the
real `/api/cards/webhook/mock` handler, in process (no running backend needed).
Without `--settle` only the authorization is sent: the card reads `charged`
and the run's leg stays in flight (an authorization is a hold, not money
moved). With `--settle` the handler records the settled charge on the run
payment and re-dispatches the payment sync, which moves a fully charged
invoice to `paid`.

    python scripts/simulate_card_charge.py --tenant acme --card <card id or token>
    python scripts/simulate_card_charge.py --tenant acme --card <id> --amount 120.00 --settle

Local only: refused when FEOH_ENVIRONMENT is a deployed one, for a tenant whose
card provider is not `mock`, and for a card that is not a mock card
(decisions §541). Root shortcut: `pnpm simulate:card -- --tenant acme --card <id>`.
"""

import argparse
import asyncio
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.mock_card_simulator import (  # noqa: E402
    MockCardSimulationRefused,
    simulate_mock_card_charge,
)

#: The simulation was refused (the code is printed).
EXIT_REFUSED = 3


def _amount(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not an amount: {value!r}") from exc


async def _run(args: argparse.Namespace) -> int:
    from app.main import app

    try:
        result = await simulate_mock_card_charge(
            app, slug=args.tenant, card_ref=args.card, amount=args.amount, settle=args.settle
        )
    except MockCardSimulationRefused as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
        return EXIT_REFUSED
    print(
        f"card {result.card_id}: sent {', '.join(result.events)}; "
        f"now {result.card_status}, charged {result.amount_charged}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="tenant slug, e.g. acme")
    parser.add_argument("--card", required=True, help="virtual card id or mock card token")
    parser.add_argument(
        "--amount", type=_amount, default=None, help="charge in major units (default: the limit)"
    )
    parser.add_argument(
        "--settle",
        action="store_true",
        help="also send the settlement event (what takes a card-paid invoice to paid)",
    )
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
