"""Generate the frontend's Terms / Privacy version module from the backend's.

`docs/decisions.md` §495. The backend records and demands acceptance of the
versions in ``app/services/legal_acceptance.py::CURRENT_LEGAL_VERSIONS``; the
signup form, the set-password page and the in-app re-prompt send back the
versions they showed. Those two must be the same values, so the frontend READS
them from ``frontend/src/lib/legal/legalVersions.generated.ts``, which this
writes (the `gen:pricing` shape):

    python scripts/gen_legal_versions.py            # write the file
    python scripts/gen_legal_versions.py --check     # fail if stale

``--check`` is the drift guard CI's Backend lint job runs. A version bumped in
the backend without regenerating would have every form send the old version
and every acceptance refused, so it is a red check rather than a broken signup.
A version that is not a ``YYYY-MM-DD`` date fails generation outright.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported (the
# same reason as `gen_pricing_catalog.py`: a worktree reusing the primary
# checkout's `.venv` would otherwise read the OTHER checkout's source).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.legal_acceptance import CURRENT_LEGAL_VERSIONS  # noqa: E402

#: Relative to the repo root so a worktree writes into its own frontend.
OUTPUT_PATH = Path("frontend/src/lib/legal/legalVersions.generated.ts")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def render() -> str:
    for document, version in CURRENT_LEGAL_VERSIONS.items():
        if not _DATE.match(version):
            raise ValueError(f"{document} version {version!r} is not YYYY-MM-DD")
    return "\n".join(
        [
            "// GENERATED FILE — do not edit by hand.",
            "//",
            "// Source of truth: backend/app/services/legal_acceptance.py",
            "// Regenerate:      pnpm gen:legal-versions",
            "// Drift check:     pnpm check:legal-versions  (runs in CI)",
            "//",
            "// The Terms of Service and Privacy Policy versions in force. Every accept",
            "// control sends these back, and the backend refuses any other value",
            "// (docs/decisions.md §495). Pure by design — no imports.",
            "",
            "/** Terms of Service version in force (its effective date). */",
            f"export const TERMS_VERSION = '{CURRENT_LEGAL_VERSIONS['terms']}';",
            "",
            "/** Privacy Policy version in force (its effective date). */",
            f"export const PRIVACY_VERSION = '{CURRENT_LEGAL_VERSIONS['privacy']}';",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed file differs from what would be generated",
    )
    args = parser.parse_args(argv)

    target = _repo_root() / OUTPUT_PATH
    rendered = render()

    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == rendered:
            print(f"{OUTPUT_PATH}: in sync with the backend's legal versions")
            return 0
        print(
            f"{OUTPUT_PATH} is STALE — backend/app/services/legal_acceptance.py "
            "names different Terms / Privacy versions than the frontend sends.\n"
            "Run `pnpm gen:legal-versions`.",
            file=sys.stderr,
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
