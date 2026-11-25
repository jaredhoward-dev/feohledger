/**
 * What an accept control sends once ticked: the Terms / Privacy versions in
 * force, read from the module `pnpm gen:legal-versions` writes from the backend
 * (`docs/decisions.md` §495), so the version a visitor accepts is the one the
 * backend records.
 */
import { PRIVACY_VERSION, TERMS_VERSION } from './legalVersions.generated.ts';

export const ACCEPTED_LEGAL_VERSIONS = {
	accepted_terms_version: TERMS_VERSION,
	accepted_privacy_version: PRIVACY_VERSION
} as const;

/**
 * What the supplier portal's set-password box sends once ticked: the Privacy
 * Policy version alone. A Supplier User is not a party to the Terms, so the
 * portal never presents them (`docs/decisions.md` §499).
 */
export const ACKNOWLEDGED_PORTAL_VERSIONS = {
	accepted_privacy_version: PRIVACY_VERSION
} as const;
