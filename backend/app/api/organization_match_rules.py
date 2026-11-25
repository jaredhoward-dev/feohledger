"""The audited writer of `settings.matching` — PO-match tolerance and the
inspection requirement, org-wide and per vendor / per commodity (decisions §512).

`services/matching_rules.resolve_match_rule` reads the block; this router is
the one way to change it. Before it, the only writer was the generic
`PATCH /api/organization`, which writes no audit row and replaces the block
whole, so loosening a vendor's tolerance — letting more of its invoices pass
the PO match unflagged — left no record of who did it. Here every change is
admin-only, validated, made under the organization row lock with the rest of
the block carried, and recorded FIRST: an audit row that cannot be written is
a 503 with nothing saved (`record_auth_audit_or_raise`, the §502 rule). The
generic PATCH refuses any change to the block (`guard_matching_block`).

Every read goes through the resolver's own coercions (`_coerce_tolerance` /
`_coerce_inspection`), so the screen shows exactly what the PO match applies —
a stored value the resolver ignores reads back as "not set", never as a rule
that is not in force. A stored tolerance the resolver DOES apply but this writer
would refuse (outside 0–100: written by the generic PATCH before it was closed
to this block) reads back as it is, with `tolerance_out_of_range: true`, so the
panel can say so and ask for a fix. It is never rewritten silently: the PO match
applies it until an admin saves a valid value.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_serializer,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, require_roles
from app.api.organization_controls import AUDIT_UNWRITABLE_DETAIL
from app.api.refusals import coded_refusal
from app.database import get_control_db
from app.models.gl_account import GL_CODE_MAX_LENGTH, GLAccount
from app.models.organization import Organization
from app.models.user import User
from app.models.vendor import Vendor
from app.services.audit_dispatch import record_auth_audit_or_raise
from app.services.matching_rules import (
    DEFAULT_REQUIRE_INSPECTION,
    DEFAULT_TOLERANCE_PCT,
    _coerce_inspection,
    _coerce_tolerance,
)
from app.tenant import get_tenant, get_tenant_db, lock_organization

router = APIRouter(prefix="/organization/match-rules", tags=["organization"])

MATCH_RULES_PATH = "/api/organization/match-rules"
BLOCK = "matching"
VENDOR_RULES = "vendor_rules"
COMMODITY_RULES = "commodity_rules"

#: Bounds of a tolerance, in percent. 0 = the invoice must equal the PO exactly;
#: 100 = it may be up to twice the PO. Two decimal places is the finest step any
#: AP policy states a percentage in.
TOLERANCE_MIN = Decimal("0")
TOLERANCE_MAX = Decimal("100")
TOLERANCE_PLACES = 2


# ---------- validation (coded refusals) --------------------------------------


def _refuse(status: int, code: str, message: str, **params: object) -> HTTPException:
    return HTTPException(status_code=status, detail=coded_refusal(code, message, **params))


def parse_tolerance(raw: object) -> Decimal | None:
    """The tolerance a request names, as exact Decimal, or a coded 422.

    `None` means "not set at this layer" (inherit). A number or a decimal string
    is accepted; a bool, a non-finite value, one outside 0–100 or one finer than
    two decimal places is refused — a value the resolver would drop silently
    falls through to a LOOSER layer, so it must never be stored."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise _refuse(422, "match_tolerance_invalid", "The tolerance must be a number.")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise _refuse(422, "match_tolerance_invalid", "The tolerance must be a number.") from None
    if not value.is_finite():
        raise _refuse(422, "match_tolerance_invalid", "The tolerance must be a number.")
    if value < TOLERANCE_MIN or value > TOLERANCE_MAX:
        raise _refuse(
            422,
            "match_tolerance_out_of_range",
            f"The tolerance must be between {TOLERANCE_MIN} and {TOLERANCE_MAX} percent.",
            min=str(TOLERANCE_MIN),
            max=str(TOLERANCE_MAX),
        )
    if value.as_tuple().exponent < -TOLERANCE_PLACES:  # type: ignore[operator]
        raise _refuse(
            422,
            "match_tolerance_too_precise",
            f"The tolerance may have at most {TOLERANCE_PLACES} decimal places.",
            places=TOLERANCE_PLACES,
        )
    return value


def _stored_tolerance(value: Decimal | None) -> str | None:
    """The exact decimal string stored in JSONB (`"2.5"`, `"0"`, `"10"`)."""
    if value is None:
        return None
    return format(value.normalize(), "f")


# ---------- schemas -----------------------------------------------------------


class MatchRuleBody(BaseModel):
    """One layer's two knobs. `null` = not set here: the field inherits from the
    next layer down (vendor → commodity → organization → built-in)."""

    model_config = ConfigDict(extra="forbid")

    tolerance_pct: StrictStr | StrictInt | StrictFloat | None = None
    require_inspection: StrictBool | None = None


class MatchRuleOut(BaseModel):
    tolerance_pct: Decimal | None
    require_inspection: bool | None
    #: The stored tolerance is applied by the PO match but lies outside the
    #: 0–100 range this writer accepts (an old unaudited write). Reported, never
    #: rewritten: the screen flags it and asks an admin to save a valid value.
    tolerance_out_of_range: bool = False

    @field_serializer("tolerance_pct")
    def _ser_tol(self, v: Decimal | None) -> str | None:
        return _stored_tolerance(v)


class VendorRuleOut(MatchRuleOut):
    vendor_id: str
    #: `None` when the vendor no longer exists in this workspace (the rule is
    #: still in force for that id and can be removed here).
    vendor_name: str | None


class CommodityRuleOut(MatchRuleOut):
    gl_account: str
    #: The chart's name for the code, when the chart has it.
    gl_account_name: str | None


class BuiltInOut(BaseModel):
    tolerance_pct: Decimal
    require_inspection: bool

    @field_serializer("tolerance_pct")
    def _ser_tol(self, v: Decimal) -> str:
        return _stored_tolerance(v) or "0"


class MatchRulesOut(BaseModel):
    organization: MatchRuleOut
    built_in: BuiltInOut
    vendor_rules: list[VendorRuleOut]
    commodity_rules: list[CommodityRuleOut]


# ---------- reading -----------------------------------------------------------


def _block(org_settings: dict | None) -> dict:
    block = (org_settings or {}).get(BLOCK)
    return block if isinstance(block, dict) else {}


def _rules(block: dict, kind: str) -> dict[str, dict]:
    rules = block.get(kind)
    if not isinstance(rules, dict):
        return {}
    return {k: v for k, v in rules.items() if isinstance(k, str) and isinstance(v, dict)}


def _effective(rule: dict) -> dict:
    """A layer as the resolver reads it — `None` where it would fall through."""
    return {
        "tolerance_pct": _coerce_tolerance(rule),
        "require_inspection": _coerce_inspection(rule),
    }


def tolerance_out_of_range(value: Decimal | None) -> bool:
    """A tolerance the resolver applies that `parse_tolerance` would refuse."""
    return value is not None and (value < TOLERANCE_MIN or value > TOLERANCE_MAX)


def _view(rule: dict) -> dict:
    """`_effective` plus the out-of-range flag, for the read model."""
    eff = _effective(rule)
    return {**eff, "tolerance_out_of_range": tolerance_out_of_range(eff["tolerance_pct"])}


async def _read(org: Organization, tdb: AsyncSession) -> MatchRulesOut:
    block = _block(org.settings)
    vendor_rules = _rules(block, VENDOR_RULES)
    commodity_rules = _rules(block, COMMODITY_RULES)

    vendor_uuids = []
    for key in vendor_rules:
        try:
            vendor_uuids.append(uuid.UUID(key))
        except ValueError:
            continue
    names: dict[str, str] = {}
    if vendor_uuids:
        rows = await tdb.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(vendor_uuids)))
        names = {str(vid): name for vid, name in rows.all()}
    gl_names: dict[str, str] = {}
    if commodity_rules:
        rows = await tdb.execute(
            select(GLAccount.code, GLAccount.name)
            .where(GLAccount.code.in_(list(commodity_rules)))
            .order_by(GLAccount.entity_id.is_not(None))
        )
        for code, name in rows.all():
            gl_names.setdefault(code, name)

    return MatchRulesOut(
        organization=MatchRuleOut(**_view(block)),
        built_in=BuiltInOut(
            tolerance_pct=DEFAULT_TOLERANCE_PCT, require_inspection=DEFAULT_REQUIRE_INSPECTION
        ),
        vendor_rules=sorted(
            (
                VendorRuleOut(vendor_id=k, vendor_name=names.get(k), **_view(v))
                for k, v in vendor_rules.items()
            ),
            key=lambda r: ((r.vendor_name or "").casefold(), r.vendor_id),
        ),
        commodity_rules=sorted(
            (
                CommodityRuleOut(gl_account=k, gl_account_name=gl_names.get(k), **_view(v))
                for k, v in commodity_rules.items()
            ),
            key=lambda r: r.gl_account,
        ),
    )


# ---------- writing -----------------------------------------------------------


def _layer_value(body: MatchRuleBody) -> dict:
    """The stored dict for one layer: only the fields that are set."""
    tolerance = parse_tolerance(body.tolerance_pct)
    out: dict = {}
    if tolerance is not None:
        out["tolerance_pct"] = _stored_tolerance(tolerance)
    if body.require_inspection is not None:
        out["require_inspection"] = body.require_inspection
    return out


def _audit_view(layer: dict | None) -> dict | None:
    """A layer as the audit row records it: the effective fields, exact strings."""
    if layer is None:
        return None
    eff = _effective(layer)
    return {
        "tolerance_pct": _stored_tolerance(eff["tolerance_pct"]),
        "require_inspection": eff["require_inspection"],
    }


async def _write(
    db: AsyncSession,
    org: Organization,
    user: User,
    *,
    kind: str | None,
    key: str | None,
    new_layer: dict | None,
) -> Organization:
    """Under the org row lock, audit FIRST, then write one layer of the block.

    `kind`/`key` name a vendor or commodity rule (`new_layer=None` removes it);
    `kind=None` is the organization layer, whose two keys sit at the top of the
    block beside the rule maps. A save that changes nothing writes nothing."""
    org = await lock_organization(db, org)
    block = dict(_block(org.settings))
    if kind is None:
        before = {f: block[f] for f in ("tolerance_pct", "require_inspection") if f in block}
        if before == (new_layer or {}):
            return org
        old_view, new_view = _audit_view(before), _audit_view(new_layer or {})
        for f in ("tolerance_pct", "require_inspection"):
            block.pop(f, None)
        block.update(new_layer or {})
        setting = f"{BLOCK}.organization"
        action = "organization.match_rule_updated"
    else:
        rules = dict(_rules(block, kind))
        before_layer = rules.get(key)  # type: ignore[arg-type]
        if before_layer == new_layer:
            return org
        old_view, new_view = _audit_view(before_layer), _audit_view(new_layer)
        if new_layer is None:
            rules.pop(key, None)  # type: ignore[arg-type]
            action = "organization.match_rule_removed"
        else:
            rules[key] = new_layer  # type: ignore[index]
            action = "organization.match_rule_updated"
        block[kind] = rules
        setting = f"{BLOCK}.{kind}.{key}"
    try:
        await record_auth_audit_or_raise(
            organization_id=org.id,
            actor_id=user.id,
            action=action,
            entity_type="organization",
            entity_id=org.id,
            details={
                "setting": setting,
                "scope": {VENDOR_RULES: "vendor", COMMODITY_RULES: "commodity"}.get(
                    kind or "", "organization"
                ),
                "key": key,
                "old": old_view,
                "new": new_view,
            },
        )
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=503, detail=AUDIT_UNWRITABLE_DETAIL) from None
    settings_out = dict(org.settings or {})
    settings_out[BLOCK] = block
    org.settings = settings_out
    flag_modified(org, "settings")
    await db.commit()
    return org


def _require_some_field(layer: dict) -> None:
    if not layer:
        raise _refuse(
            422,
            "match_rule_empty",
            "Set a tolerance, an inspection requirement, or both.",
        )


async def _require_vendor(tdb: AsyncSession, vendor_id: uuid.UUID) -> None:
    found = (await tdb.execute(select(Vendor.id).where(Vendor.id == vendor_id))).first()
    if found is None:
        raise _refuse(404, "match_rule_vendor_not_found", "That vendor does not exist.")


async def _require_gl_code(tdb: AsyncSession, code: str) -> None:
    if not code or len(code) > GL_CODE_MAX_LENGTH or code != code.strip():
        raise _refuse(
            404, "match_rule_gl_account_not_found", "That GL account is not in the chart."
        )
    found = (await tdb.execute(select(GLAccount.id).where(GLAccount.code == code))).first()
    if found is None:
        raise _refuse(
            404, "match_rule_gl_account_not_found", "That GL account is not in the chart."
        )


def guard_matching_block(incoming: dict, stored: dict | None) -> None:
    """Keep the generic `PATCH /api/organization` from being a second,
    unaudited writer of `settings.matching`: a save that names the block must
    carry it unchanged, or it is a 422 naming this router."""
    if BLOCK not in incoming:
        return
    if incoming[BLOCK] != (stored or {}).get(BLOCK):
        raise HTTPException(
            status_code=422,
            detail=(
                f"settings.{BLOCK} is changed through {MATCH_RULES_PATH}, which audits "
                "the change; leave it out of this save."
            ),
        )


# ---------- routes ------------------------------------------------------------


@router.get("", response_model=MatchRulesOut)
async def get_match_rules(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    tdb: AsyncSession = Depends(get_tenant_db),
):
    """The organization layer, the built-in fallback and every vendor and
    commodity rule, each field as the PO match reads it (`null` = inherits)."""
    return await _read(org, tdb)


@router.put("/organization", response_model=MatchRulesOut)
async def put_org_match_rule(
    body: MatchRuleBody,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
    tdb: AsyncSession = Depends(get_tenant_db),
):
    """Set the organization layer. A `null` field falls back to the built-in."""
    layer = _layer_value(body)
    org = await _write(db, org, user, kind=None, key=None, new_layer=layer)
    return await _read(org, tdb)


@router.put("/vendors/{vendor_id}", response_model=MatchRulesOut)
async def put_vendor_match_rule(
    vendor_id: uuid.UUID,
    body: MatchRuleBody,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
    tdb: AsyncSession = Depends(get_tenant_db),
):
    """Create or replace one vendor's rule. It outranks every commodity rule."""
    layer = _layer_value(body)
    _require_some_field(layer)
    await _require_vendor(tdb, vendor_id)
    org = await _write(db, org, user, kind=VENDOR_RULES, key=str(vendor_id), new_layer=layer)
    return await _read(org, tdb)


@router.delete("/vendors/{vendor_id}", response_model=MatchRulesOut)
async def delete_vendor_match_rule(
    vendor_id: uuid.UUID,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
    tdb: AsyncSession = Depends(get_tenant_db),
):
    """Remove a vendor's rule (idempotent: an absent rule is a no-op)."""
    org = await _write(db, org, user, kind=VENDOR_RULES, key=str(vendor_id), new_layer=None)
    return await _read(org, tdb)


@router.put("/commodities/{gl_account:path}", response_model=MatchRulesOut)
async def put_commodity_match_rule(
    gl_account: str,
    body: MatchRuleBody,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
    tdb: AsyncSession = Depends(get_tenant_db),
):
    """Create or replace the rule for one header GL account (the commodity)."""
    layer = _layer_value(body)
    _require_some_field(layer)
    await _require_gl_code(tdb, gl_account)
    org = await _write(db, org, user, kind=COMMODITY_RULES, key=gl_account, new_layer=layer)
    return await _read(org, tdb)


@router.delete("/commodities/{gl_account:path}", response_model=MatchRulesOut)
async def delete_commodity_match_rule(
    gl_account: str,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
    tdb: AsyncSession = Depends(get_tenant_db),
):
    """Remove a commodity rule (idempotent)."""
    org = await _write(db, org, user, kind=COMMODITY_RULES, key=gl_account, new_layer=None)
    return await _read(org, tdb)
