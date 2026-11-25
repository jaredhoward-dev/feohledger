"""Acceptance of our Terms of Service and Privacy Policy (`docs/decisions.md` §495).

``CURRENT_LEGAL_VERSIONS`` is the single source of truth for which version of
each document is in force. The version is the document's effective date as
published. The frontend does not keep its own copy: `pnpm gen:legal-versions`
writes ``frontend/src/lib/legal/legalVersions.generated.ts`` from this dict and
`pnpm check:legal-versions` fails CI when the two differ, so the checkbox a
visitor ticks always names the version the backend will record and demand.

Bump a version here when the published document changes in a way users must
agree to again: every user whose newest acceptance is older then meets the
blocking re-prompt on their next page load (``GET /api/auth/me`` →
``legal_acceptance_required``). A typo fix on the page is not a new version.

The client sends back the versions it showed; the server refuses anything but
the current ones (``legal_acceptance_required``), so a stale tab cannot record
acceptance of a version it never displayed — and an absent field is a refusal,
never a default.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refusals import coded_refusal
from app.models.legal_acceptance import LegalAcceptance

#: document → version in force (its published effective date, YYYY-MM-DD).
CURRENT_LEGAL_VERSIONS: dict[str, str] = {
    "terms": "2026-11-21",
    "privacy": "2026-11-21",
}

LEGAL_DOCUMENTS: tuple[str, ...] = tuple(CURRENT_LEGAL_VERSIONS)

#: How the acceptance was given. Stored on the row.
SOURCE_SIGNUP = "signup"
SOURCE_SET_PASSWORD = "set_password"
SOURCE_REPROMPT = "reprompt"

#: Coded refusal: the request did not accept the versions currently in force.
LEGAL_ACCEPTANCE_REQUIRED = "legal_acceptance_required"


def acceptance_refusal() -> dict:
    return coded_refusal(
        LEGAL_ACCEPTANCE_REQUIRED,
        "Please accept the current Terms of Service and Privacy Policy to continue.",
    )


def accepts_current(terms_version: str | None, privacy_version: str | None) -> bool:
    """True only when the caller accepted exactly the versions in force."""
    return (
        terms_version == CURRENT_LEGAL_VERSIONS["terms"]
        and privacy_version == CURRENT_LEGAL_VERSIONS["privacy"]
    )


async def missing_documents(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Documents whose CURRENT version this user has not accepted, in order."""
    rows = (
        await db.execute(
            select(LegalAcceptance.document, LegalAcceptance.version).where(
                LegalAcceptance.user_id == user_id
            )
        )
    ).all()
    accepted = {(doc, ver) for doc, ver in rows}
    return [doc for doc, ver in CURRENT_LEGAL_VERSIONS.items() if (doc, ver) not in accepted]


async def record_acceptances(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    versions: Mapping[str, str],
    source: str,
    accepted_at: datetime | None = None,
) -> None:
    """Insert one row per (document, version) accepted, in the caller's transaction.

    Append-only: an acceptance already on record is left as it was (its first
    ``accepted_at`` is the evidence), never overwritten. The caller commits.
    ``accepted_at`` defaults to now; signup passes the moment the form was
    submitted, since the account the row names is only created later.
    """
    for document, version in versions.items():
        if document not in CURRENT_LEGAL_VERSIONS:
            raise ValueError(f"unknown legal document {document!r}")
        await db.execute(
            pg_insert(LegalAcceptance)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                organization_id=organization_id,
                document=document,
                version=version,
                source=source,
                **({"accepted_at": accepted_at} if accepted_at is not None else {}),
            )
            .on_conflict_do_nothing(constraint="uq_legal_acceptances_user_doc_version")
        )
