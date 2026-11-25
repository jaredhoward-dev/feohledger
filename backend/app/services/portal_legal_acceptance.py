"""A supplier-portal user's acknowledgement of our Privacy Policy (`docs/decisions.md` §499).

The portal twin of ``services/legal_acceptance`` (§495), scoped to what the
published documents say applies to a supplier:

* **The Terms of Service are not presented.** They are "a binding agreement
  between the organisation that subscribes to or uses" the service and us; a
  Supplier User "is not our customer; access is granted by, and on the
  responsibility of, the Customer", and the Customer "must ensure that
  everyone it gives access complies with these Terms" (`/legal/terms` §1, §2,
  §3.4). Asking a supplier to "accept" them would, under §1's "where no
  organisation is named, the individual accepting is the Customer", purport to
  make them a party. Whether the portal needs terms of use binding suppliers
  directly is counsel's call, tracked in ``docs/followups.md``.
* **The Privacy Policy is acknowledged, not agreed to.** Its section 3 is
  written for suppliers ("If you are a supplier…"), and it is a notice: the
  record says the supplier was shown the version in force, nothing more.

The version comes from ``legal_acceptance.CURRENT_LEGAL_VERSIONS`` — one owner
for what is in force, so the frontend's generated module stays the only copy.
The client echoes the version it showed; only the version in force passes
(422 ``privacy_acknowledgement_required``), never a default.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refusals import coded_refusal
from app.models.vendor_user_legal_acceptance import VendorUserLegalAcceptance
from app.services.legal_acceptance import (
    CURRENT_LEGAL_VERSIONS,
    SOURCE_SET_PASSWORD,
)

#: The documents a supplier-portal user is shown, in order (§499).
PORTAL_LEGAL_DOCUMENTS: tuple[str, ...] = ("privacy",)

#: Coded refusal: the request did not acknowledge the Privacy Policy in force.
PRIVACY_ACKNOWLEDGEMENT_REQUIRED = "privacy_acknowledgement_required"


def current_portal_versions() -> dict[str, str]:
    """document → version in force, for the documents a supplier is shown."""
    return {doc: CURRENT_LEGAL_VERSIONS[doc] for doc in PORTAL_LEGAL_DOCUMENTS}


def acknowledgement_refusal() -> dict:
    return coded_refusal(
        PRIVACY_ACKNOWLEDGEMENT_REQUIRED,
        "Please confirm you have read the current Privacy Policy to continue.",
    )


def acknowledges_current(privacy_version: str | None) -> bool:
    """True only when the caller acknowledged exactly the version in force."""
    return privacy_version == CURRENT_LEGAL_VERSIONS["privacy"]


async def missing_documents(db: AsyncSession, vendor_user_id: uuid.UUID) -> list[str]:
    """Portal documents whose CURRENT version this vendor user has not acknowledged."""
    rows = (
        await db.execute(
            select(VendorUserLegalAcceptance.document, VendorUserLegalAcceptance.version).where(
                VendorUserLegalAcceptance.vendor_user_id == vendor_user_id
            )
        )
    ).all()
    seen = {(doc, ver) for doc, ver in rows}
    return [doc for doc, ver in current_portal_versions().items() if (doc, ver) not in seen]


async def record_acknowledgement(
    db: AsyncSession,
    *,
    vendor_user_id: uuid.UUID,
    source: str = SOURCE_SET_PASSWORD,
) -> None:
    """Insert one row per portal document in force, in the caller's transaction.

    Append-only: an acknowledgement already on record keeps its first
    ``accepted_at``, never overwritten. The caller commits.
    """
    for document, version in current_portal_versions().items():
        await db.execute(
            pg_insert(VendorUserLegalAcceptance)
            .values(
                id=uuid.uuid4(),
                vendor_user_id=vendor_user_id,
                document=document,
                version=version,
                source=source,
            )
            .on_conflict_do_nothing(constraint="uq_vendor_user_legal_acceptances_doc_version")
        )
