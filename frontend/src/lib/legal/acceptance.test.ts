import { describe, expect, it } from 'vitest';

import { ACCEPTED_LEGAL_VERSIONS } from './acceptance.ts';
import { PRIVACY_VERSION, TERMS_VERSION } from './legalVersions.generated.ts';
import { LAST_UPDATED } from './operator.ts';

describe('accepted legal versions (decisions §495)', () => {
	it('sends exactly the generated versions', () => {
		expect(ACCEPTED_LEGAL_VERSIONS).toEqual({
			accepted_terms_version: TERMS_VERSION,
			accepted_privacy_version: PRIVACY_VERSION
		});
	});

	it('never names a version newer than the published set', () => {
		// A version is the document's effective date. One later than the set's
		// last-updated stamp would ask users to accept text not yet published.
		for (const v of [TERMS_VERSION, PRIVACY_VERSION]) {
			expect(v).toMatch(/^\d{4}-\d{2}-\d{2}$/);
			expect(v <= LAST_UPDATED).toBe(true);
		}
	});
});
