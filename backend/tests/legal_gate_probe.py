"""Shared probe for "a token door refuses a reviewer who owes a Terms / Privacy
acceptance" (decisions §505).

The email link and the Slack / Teams buttons act for a harness user on a signed
token, so the test needs that user to owe the acceptance for a moment and then
to have given it. `acceptance_withdrawn` deletes the user's control-plane
acceptance rows, yields an ``accept()`` that records the versions in force
again (the way `conftest._test_users_accept_the_legal_versions` does), and
always restores them on exit, since harness users persist across tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.legal_acceptance import LegalAcceptance
from app.services.legal_acceptance import CURRENT_LEGAL_VERSIONS


@asynccontextmanager
async def acceptance_withdrawn(
    realdb, user_id: uuid.UUID, *, key: str = "a"
) -> AsyncIterator[Callable[[], Awaitable[None]]]:
    mk = realdb.control_sessionmaker()
    org_id = realdb.info(key).org_id

    async def accept() -> None:
        async with mk() as s:
            for document, version in CURRENT_LEGAL_VERSIONS.items():
                await s.execute(
                    pg_insert(LegalAcceptance)
                    .values(
                        id=uuid.uuid4(),
                        user_id=user_id,
                        organization_id=org_id,
                        document=document,
                        version=version,
                        source="seed",
                    )
                    .on_conflict_do_nothing(constraint="uq_legal_acceptances_user_doc_version")
                )
            await s.commit()

    async with mk() as s:
        await s.execute(delete(LegalAcceptance).where(LegalAcceptance.user_id == user_id))
        await s.commit()
    try:
        yield accept
    finally:
        await accept()
