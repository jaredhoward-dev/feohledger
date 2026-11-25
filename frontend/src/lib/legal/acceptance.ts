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
