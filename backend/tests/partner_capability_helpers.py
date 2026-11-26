"""Set a realdb tenant's operator-granted partner capability (decisions §574).

The realdb control-plane rows persist across the session, so a test that makes
an org a partner resets it afterwards (`granted_partner` below).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import update

from app.models.organization import Organization


async def set_is_partner(realdb, key: str, value: bool) -> None:
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            update(Organization)
            .where(Organization.id == realdb.info(key).org_id)
            .values(is_partner=value)
        )
        await s.commit()


@asynccontextmanager
async def granted_partner(realdb, key: str = "a"):
    """Tenant ``key`` holds the partner capability for the block's duration."""
    await set_is_partner(realdb, key, True)
    try:
        yield
    finally:
        await set_is_partner(realdb, key, False)
