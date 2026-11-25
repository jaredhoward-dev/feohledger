/**
 * PO-match rules (admin only) — `backend/app/api/organization_match_rules.py`,
 * decisions §512. Every write is audited and answers with the whole rule set
 * as the PO match now reads it; a 503 means the audit row could not be
 * written and nothing was saved.
 */
import { api } from '#lib/api.ts';
import type { MatchRuleBody, MatchRules } from '#lib/types/matchRules.ts';

const BASE = '/api/organization/match-rules';

export function getMatchRules(): Promise<MatchRules> {
	return api.get<MatchRules>(BASE);
}

export function saveOrganizationMatchRule(body: MatchRuleBody): Promise<MatchRules> {
	return api.put<MatchRules>(`${BASE}/organization`, body);
}

export function saveVendorMatchRule(vendorId: string, body: MatchRuleBody): Promise<MatchRules> {
	return api.put<MatchRules>(`${BASE}/vendors/${encodeURIComponent(vendorId)}`, body);
}

export function deleteVendorMatchRule(vendorId: string): Promise<MatchRules> {
	return api.delete<MatchRules>(`${BASE}/vendors/${encodeURIComponent(vendorId)}`);
}

export function saveCommodityMatchRule(glCode: string, body: MatchRuleBody): Promise<MatchRules> {
	return api.put<MatchRules>(`${BASE}/commodities/${encodeURIComponent(glCode)}`, body);
}

export function deleteCommodityMatchRule(glCode: string): Promise<MatchRules> {
	return api.delete<MatchRules>(`${BASE}/commodities/${encodeURIComponent(glCode)}`);
}
