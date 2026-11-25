"""Audited writers for two single-key organization controls (decisions §502).

* `settings.fraud_rules.line_total_mismatch_enabled` — whether an invoice whose
  line totals do not add up to its header amount raises the payment-blocking
  `line_total_mismatch` warning + exception (`services/invoice_warnings`).
* `settings.exception_agents.autonomy_level` — how confident an exception agent
  must be before it resolves an exception itself instead of escalating it
  (`services/exception_agents/autonomy`). Above `conservative` an agent may
  change an invoice (its amount to the PO total, its PO link, its GL account)
  and approve it, as the user who ran it.

Both used to be reachable only through the generic `PATCH /api/organization`,
which writes no audit row and replaces each top-level block whole. Here each is
admin-only, validated, written under the organization row lock with its
siblings carried, and recorded FIRST: an audit row that cannot be written is a
503 with nothing saved (`record_auth_audit_or_raise`, the `/organization/sso`
rule). The generic PATCH carries both stored values across and refuses a
different one (`guard_owned_keys`), so these are the one way to change them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, get_args

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, require_roles
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.services.audit_dispatch import record_auth_audit_or_raise
from app.services.exception_agents.autonomy import resolve_autonomy_level
from app.services.invoice_warnings import DEFAULT_FRAUD_RULES
from app.tenant import get_tenant, lock_organization

router = APIRouter(prefix="/organization", tags=["organization"])

LINE_TOTAL_RULE = "line_total_mismatch_enabled"
AUTONOMY_KEY = "autonomy_level"

LINE_TOTAL_PATH = "/api/organization/line-total-check"
AUTONOMY_PATH = "/api/organization/exception-agents/autonomy"

AUDIT_UNWRITABLE_DETAIL = (
    "The change could not be recorded in the audit trail, so it was not saved."
)

AutonomyLevel = Literal["conservative", "balanced", "aggressive"]
AUTONOMY_LEVELS: frozenset[str] = frozenset(get_args(AutonomyLevel))


class LineTotalCheckSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class AutonomySetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    autonomy_level: AutonomyLevel


def line_total_check_enabled(org_settings: dict | None) -> bool:
    """The rule exactly as the warning engine reads it (`invoice_warnings`
    `_fraud_config` + `if cfg[...]`): the stored value, else the default, taken
    for its truthiness. A stored `0` / `None` / `""` turns the engine's check
    off, so it must read off here too — reporting it on would hide a disabled
    payment control and turn the PUT that re-enables it into a no-op."""
    rules = (org_settings or {}).get("fraud_rules") or {}
    value = rules.get(LINE_TOTAL_RULE, DEFAULT_FRAUD_RULES[LINE_TOTAL_RULE])
    return bool(value)


def _owned_value_refusal(block: str, key: str, path: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=(
            f"{block}.{key} is changed through {path}, which audits the "
            "change; leave it out of this save."
        ),
    )


def guard_owned_keys(incoming: dict, stored: dict | None) -> None:
    """Keep the generic `PATCH /api/organization` from being a second,
    unaudited writer of the two keys this module owns.

    It replaces each top-level block whole, so a `fraud_rules` or
    `exception_agents` save that omits the key would drop it, and one that
    names a different value would change it unrecorded. So:

    * a block that is present but not an object (`null`, a string, a list) is
      a 422 — it would replace the whole block, owned key included, and a
      non-object block crashes every later reader of it;
    * an omitted key gets the stored value carried across;
    * a named key must be the canonical type — a real bool for the line-total
      rule, one of the known levels for autonomy (`0 == False` is not an
      equal value here) — and equal to the effective one, or it is a 422
      naming the endpoint that owns it.

    Mutates `incoming` in place.
    """
    stored = stored or {}
    for block, key, effective, canonical, path in (
        (
            "fraud_rules",
            LINE_TOTAL_RULE,
            line_total_check_enabled(stored),
            lambda v: type(v) is bool,
            LINE_TOTAL_PATH,
        ),
        (
            "exception_agents",
            AUTONOMY_KEY,
            resolve_autonomy_level(stored),
            lambda v: isinstance(v, str) and v in AUTONOMY_LEVELS,
            AUTONOMY_PATH,
        ),
    ):
        if block not in incoming:
            continue
        new_block = incoming[block]
        if not isinstance(new_block, dict):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"settings.{block} must be an object; {block}.{key} is changed through {path}."
                ),
            )
        if key in new_block:
            value = new_block[key]
            if not canonical(value) or value != effective:
                raise _owned_value_refusal(block, key, path)
            continue
        stored_block = stored.get(block)
        if isinstance(stored_block, dict) and key in stored_block:
            incoming[block] = {**new_block, key: stored_block[key]}


async def _set_one_key(
    db: AsyncSession,
    org: Organization,
    user: User,
    *,
    block: str,
    key: str,
    after: object,
    effective: Callable[[dict | None], object],
    action: str,
) -> Organization:
    """Under the org row lock: read the effective value, and when it differs —
    or the stored value reads the same but is not the canonical one (a `0`
    where the rule wants `False`, an unknown level read as `conservative`) —
    write the audit row FIRST, then the one key with its block's siblings
    carried. Returns the locked, refreshed organization."""
    org = await lock_organization(db, org)
    before = effective(org.settings)
    stored_block = (org.settings or {}).get(block)
    has_raw = isinstance(stored_block, dict) and key in stored_block
    raw = stored_block[key] if has_raw else None
    canonical = not has_raw or (type(raw) is type(after) and raw == after)
    if before == after and canonical:
        return org
    details: dict = {"setting": f"{block}.{key}", "old": before, "new": after}
    if not canonical:
        # The effective value may not move; record what was rewritten.
        details["stored"] = raw
    try:
        await record_auth_audit_or_raise(
            organization_id=org.id,
            actor_id=user.id,
            action=action,
            entity_type="organization",
            entity_id=org.id,
            details=details,
        )
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=503, detail=AUDIT_UNWRITABLE_DETAIL) from None
    existing = dict(org.settings or {})
    existing[block] = {**(stored_block if isinstance(stored_block, dict) else {}), key: after}
    org.settings = existing
    flag_modified(org, "settings")
    await db.commit()
    return org


@router.get("/line-total-check", response_model=LineTotalCheckSetting)
async def get_line_total_check(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Whether the line-total reconciliation check is on (effective value)."""
    return LineTotalCheckSetting(enabled=line_total_check_enabled(org.settings))


@router.put("/line-total-check", response_model=LineTotalCheckSetting)
async def update_line_total_check(
    body: LineTotalCheckSetting,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Turn the line-total reconciliation check on or off. Admin only; audited
    (`organization.fraud_rule_updated`, old and new value). Takes effect on
    each invoice's next warning refresh."""
    org = await _set_one_key(
        db,
        org,
        user,
        block="fraud_rules",
        key=LINE_TOTAL_RULE,
        after=body.enabled,
        effective=line_total_check_enabled,
        action="organization.fraud_rule_updated",
    )
    return LineTotalCheckSetting(enabled=line_total_check_enabled(org.settings))


@router.get("/exception-agents/autonomy", response_model=AutonomySetting)
async def get_exception_agent_autonomy(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """The autonomy level in force (unknown / absent reads `conservative`)."""
    return AutonomySetting(autonomy_level=resolve_autonomy_level(org.settings))


@router.put("/exception-agents/autonomy", response_model=AutonomySetting)
async def update_exception_agent_autonomy(
    body: AutonomySetting,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Set how confident an exception agent must be to act on its own. Admin
    only (decisions §502); audited (`organization.exception_agent_autonomy_updated`,
    old and new level)."""
    org = await _set_one_key(
        db,
        org,
        user,
        block="exception_agents",
        key=AUTONOMY_KEY,
        after=body.autonomy_level,
        effective=resolve_autonomy_level,
        action="organization.exception_agent_autonomy_updated",
    )
    return AutonomySetting(autonomy_level=resolve_autonomy_level(org.settings))
