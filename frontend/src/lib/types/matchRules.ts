// PO-match rules — `settings.matching`, written only through the audited
// `/api/organization/match-rules` router
// (`backend/app/api/organization_match_rules.py`, decisions §512) and read by
// `backend/app/services/matching_rules.resolve_match_rule`.
//
// Every field is the value the resolver applies at that layer, or `null` when
// the layer does not set it and the next one down decides: vendor rule →
// commodity (header GL account) rule → organization → built-in. Tolerances are
// exact decimal strings in percentage POINTS ("2.5" = 2.5%).

import type { MessageKey } from '#lib/i18n/messages.ts';

export interface MatchRuleFields {
	tolerance_pct: string | null;
	require_inspection: boolean | null;
}

export interface VendorMatchRule extends MatchRuleFields {
	vendor_id: string;
	/** `null` when the vendor no longer exists; the rule can still be removed. */
	vendor_name: string | null;
}

export interface CommodityMatchRule extends MatchRuleFields {
	gl_account: string;
	gl_account_name: string | null;
}

export interface MatchRules {
	organization: MatchRuleFields;
	built_in: { tolerance_pct: string; require_inspection: boolean };
	vendor_rules: VendorMatchRule[];
	commodity_rules: CommodityMatchRule[];
}

/** What a save sends: `null` = not set here (inherit). */
export interface MatchRuleBody {
	tolerance_pct: string | null;
	require_inspection: boolean | null;
}

/** The inspection select's three states, as a form value. */
export type InspectionChoice = 'inherit' | 'required' | 'not_required';

export function inspectionChoice(value: boolean | null): InspectionChoice {
	if (value === null) return 'inherit';
	return value ? 'required' : 'not_required';
}

export function inspectionFromChoice(choice: InspectionChoice): boolean | null {
	if (choice === 'inherit') return null;
	return choice === 'required';
}

const INSPECTION_KEYS: Record<InspectionChoice, MessageKey> = {
	inherit: 'org.matchRules.inspection.inherit',
	required: 'org.matchRules.inspection.required',
	not_required: 'org.matchRules.inspection.notRequired'
};

export function inspectionLabelKey(choice: InspectionChoice): MessageKey {
	return INSPECTION_KEYS[choice];
}

/**
 * The tolerance text a form submits: trimmed, `null` when blank (inherit). The
 * value is NOT range-checked here — the server owns those rules and answers
 * with a coded refusal the page states in the reader's language. A comma
 * decimal separator ("2,5") is read as a point, since a number pad in five of
 * the six locales types one.
 */
export function toleranceBody(text: string): string | null {
	const trimmed = text.trim();
	if (!trimmed) return null;
	return /^\d+,\d+$/.test(trimmed) ? trimmed.replace(',', '.') : trimmed;
}

/**
 * The tolerance the organization layer resolves to: its own value, else the
 * built-in. This is the floor a vendor or commodity rule falls back to.
 */
export function organizationTolerance(rules: MatchRules): string {
	return rules.organization.tolerance_pct ?? rules.built_in.tolerance_pct;
}

/** Exact comparison of two non-negative decimal strings (no float). */
export function compareDecimal(a: string, b: string): number {
	const [ai, af = ''] = a.split('.');
	const [bi, bf = ''] = b.split('.');
	const intA = ai.replace(/^0+(?=\d)/, '');
	const intB = bi.replace(/^0+(?=\d)/, '');
	if (intA.length !== intB.length) return intA.length < intB.length ? -1 : 1;
	if (intA !== intB) return intA < intB ? -1 : 1;
	const width = Math.max(af.length, bf.length);
	const fa = af.padEnd(width, '0');
	const fb = bf.padEnd(width, '0');
	if (fa === fb) return 0;
	return fa < fb ? -1 : 1;
}

/**
 * A rule's own tolerance is wider than the organization's: more of this
 * vendor's (or GL account's) invoices pass the PO match unflagged. The screen
 * says so beside the rule, since that is the direction that needs a reason.
 */
export function isLooserThanOrganization(rule: MatchRuleFields, rules: MatchRules): boolean {
	if (rule.tolerance_pct === null) return false;
	return compareDecimal(rule.tolerance_pct, organizationTolerance(rules)) > 0;
}
