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
import { decimalSeparator } from '#lib/utils/quantity.ts';

export interface MatchRuleFields {
	tolerance_pct: string | null;
	require_inspection: boolean | null;
	/** The stored tolerance is applied by the PO match but lies outside the
	 *  0–100 range the writer accepts (an old unaudited write). The server
	 *  reports it and never rewrites it; the panel flags it for a fix. */
	tolerance_out_of_range?: boolean;
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
 * A stored dot-decimal tolerance as the reader writes it, to pre-fill a field
 * ("2.5" → "2,5" in de / fr / es / pt-BR); blank for `null` (inherit). The
 * separator is the shared one `utils/quantity.ts` reads from `Intl`, the same
 * one `toleranceBody` accepts back.
 */
export function toleranceInput(
	value: string | null,
	locale: string | undefined = undefined
): string {
	if (value === null) return '';
	return value.replace('.', decimalSeparator(locale));
}

/** "2.5" in the reader's notation — the example the tolerance hints quote. */
export function toleranceExample(locale: string | undefined = undefined): string {
	return `2${decimalSeparator(locale)}5`;
}

/**
 * The tolerance the organization layer resolves to: its own value, else the
 * built-in. This is the floor a vendor or commodity rule falls back to.
 */
export function organizationTolerance(rules: MatchRules): string {
	return rules.organization.tolerance_pct ?? rules.built_in.tolerance_pct;
}

/** Exact comparison of two decimal strings (no float). Signed, because an
 *  out-of-range stored tolerance ("-5") can reach it. */
export function compareDecimal(a: string, b: string): number {
	const negA = a.startsWith('-');
	const negB = b.startsWith('-');
	if (negA || negB) {
		const magA = negA ? a.slice(1) : a;
		const magB = negB ? b.slice(1) : b;
		const zeroA = !/[1-9]/.test(magA);
		const zeroB = !/[1-9]/.test(magB);
		if (negA && negB) return compareMagnitude(magB, magA);
		if (negA) return zeroA && zeroB ? 0 : -1;
		return zeroA && zeroB ? 0 : 1;
	}
	return compareMagnitude(a, b);
}

function compareMagnitude(a: string, b: string): number {
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

/** The inspection requirement the organization layer resolves to. */
export function organizationInspection(rules: MatchRules): boolean {
	return rules.organization.require_inspection ?? rules.built_in.require_inspection;
}

/**
 * A rule loosens the match against the organization's: more of this vendor's
 * (or GL account's) invoices pass unflagged. Either knob can do it — a wider
 * tolerance, or `require_inspection: false` where the organization requires
 * inspection (the invoice skips the quality hold). The screen says so beside
 * the rule, since that is the direction that needs a reason.
 */
export function isLooserThanOrganization(rule: MatchRuleFields, rules: MatchRules): boolean {
	if (rule.require_inspection === false && organizationInspection(rules)) return true;
	if (rule.tolerance_pct === null) return false;
	return compareDecimal(rule.tolerance_pct, organizationTolerance(rules)) > 0;
}
