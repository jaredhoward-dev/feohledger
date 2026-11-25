/**
 * Tenant SSO configuration (admin only).
 *
 * The client secret is write-only end to end: it goes out on
 * {@link updateSsoSettings} and never comes back on any read. Don't add a
 * helper that tries to fetch it — the backend has no endpoint that serves it.
 */
import { api } from '#lib/api.ts';
import type { StepUpProof } from '#lib/stores/auth.svelte.ts';
import type {
	ScimTokenMinted,
	SsoSettingsStatus,
	SsoSettingsUpdate
} from '#lib/types/ssoSettings.ts';

const BASE = '/api/organization/sso';

export const getSsoSettings = () => api.get<SsoSettingsStatus>(BASE);

export const updateSsoSettings = (body: SsoSettingsUpdate) =>
	api.put<SsoSettingsStatus>(BASE, body);

/** Mint (or rotate) the tenant's SCIM bearer token. Admin + the `scim`
 *  feature + a second-factor step-up (decisions §510): the server answers
 *  `sensitive_step_up_required` until `stepUp` carries a code or a passkey
 *  assertion. Audited first. The plaintext in the result is never served again. */
export const mintScimToken = (stepUp?: StepUpProof) =>
	api.post<ScimTokenMinted>(`${BASE}/scim-token`, stepUp ? { step_up: stepUp } : {});

/** Revoke the SCIM bearer token. Admin only, not plan-gated; audited first. */
export const revokeScimToken = () => api.delete<void>(`${BASE}/scim-token`);
