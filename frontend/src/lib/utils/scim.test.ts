import { describe, expect, it } from 'vitest';
import { scimActions, scimBaseUrl } from './scim.ts';

describe('scimBaseUrl', () => {
	it('uses the platform API origin when there is one', () => {
		expect(scimBaseUrl('https://api.example.com', 'https://acme.example.com')).toBe(
			'https://api.example.com/api/scim/v2'
		);
	});

	it('falls back to the page origin on a vanity host (same-origin API)', () => {
		expect(scimBaseUrl('', 'https://ap.acme.test')).toBe('https://ap.acme.test/api/scim/v2');
	});

	it('never doubles a trailing slash', () => {
		expect(scimBaseUrl('http://localhost:8000/', 'x')).toBe('http://localhost:8000/api/scim/v2');
	});
});

describe('scimActions', () => {
	it('offers generate only when no token is set and the plan has SCIM', () => {
		expect(scimActions(false, true)).toEqual(['generate']);
		expect(scimActions(false, false)).toEqual([]);
	});

	it('offers rotate and revoke over a live token', () => {
		expect(scimActions(true, true)).toEqual(['rotate', 'revoke']);
	});

	it('still offers revoke when the plan lost SCIM: revoke is not plan-gated', () => {
		expect(scimActions(true, false)).toEqual(['revoke']);
	});
});
