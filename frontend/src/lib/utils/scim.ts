/**
 * Pure helpers for the Organization → SCIM provisioning panel
 * (`ScimTokenPanel.svelte`, decisions §510).
 */

/**
 * The SCIM 2.0 base URL an identity provider is pointed at.
 *
 * SCIM resolves the tenant from the bearer token alone (`api/scim.py`), so
 * the URL is the API origin plus the fixed `/api/scim/v2` path. `apiBase` is
 * `getApiBase()`: the platform API on a platform host, `''` (same origin) on a
 * vanity domain, where `pageOrigin` stands in.
 */
export function scimBaseUrl(apiBase: string, pageOrigin: string): string {
	const origin = (apiBase || pageOrigin).replace(/\/+$/, '');
	return `${origin}/api/scim/v2`;
}

export type ScimPanelAction = 'generate' | 'rotate' | 'revoke';

/**
 * Which actions the panel offers. Generating is the plan-gated mint; rotating
 * is the same mint over a live token, so it needs the plan too. Revoking is
 * not plan-gated server-side, so a tenant that lost the feature can still
 * retire a token it minted.
 */
export function scimActions(configured: boolean, entitled: boolean): ScimPanelAction[] {
	if (!configured) return entitled ? ['generate'] : [];
	return entitled ? ['rotate', 'revoke'] : ['revoke'];
}
