import { beforeEach, describe, expect, it, vi } from 'vitest';

// The portal store's Privacy Policy acknowledgement (`docs/decisions.md` §500),
// driven against a stubbed `#lib/portalApi.ts` (the real one reaches
// `$app/env/public` through `#lib/tenant.ts`, which only SvelteKit resolves).
const get = vi.fn();
const post = vi.fn();
let token: string | null = 'tok';

vi.mock('#lib/portalApi.ts', () => ({
	portalApi: {
		get: (...args: unknown[]) => get(...args),
		post: (...args: unknown[]) => post(...args)
	},
	setPortalToken: (t: string) => {
		token = t;
	},
	clearPortalToken: () => {
		token = null;
	},
	hasPortalToken: () => token !== null
}));

const SUPPLIER = {
	id: 'vu-1',
	email: 'supplier@portal.test',
	full_name: 'Portal Demo User',
	must_change_password: false,
	mfa_enabled: false,
	vendor_id: 'v-1',
	vendor_name: 'Tech Hardware Corp',
	vendor_status: 'active'
};

async function freshStore() {
	vi.resetModules();
	token = 'tok';
	return (await import('./portalAuth.svelte.ts')).portalAuth;
}

describe('portalAuth.acknowledgeLegal — the Privacy Policy re-prompt', () => {
	beforeEach(() => {
		get.mockReset();
		post.mockReset();
	});

	it('posts only the Privacy Policy version this build carries and takes the answer as the profile', async () => {
		const { ACKNOWLEDGED_PORTAL_VERSIONS } = await import('#lib/legal/acceptance.ts');
		const portalAuth = await freshStore();
		get.mockResolvedValueOnce({ ...SUPPLIER, legal_acceptance_required: true });
		await portalAuth.fetchUser();
		expect(portalAuth.user?.legal_acceptance_required).toBe(true);

		post.mockResolvedValueOnce({ ...SUPPLIER, legal_acceptance_required: false });
		await portalAuth.acknowledgeLegal();

		expect(post).toHaveBeenCalledWith(
			'/api/portal/auth/legal-acknowledgement',
			ACKNOWLEDGED_PORTAL_VERSIONS
		);
		// No Terms: a Supplier User is not a party to them (§499).
		expect(Object.keys(post.mock.calls[0][1] as object)).toEqual(['accepted_privacy_version']);
		expect(portalAuth.user?.legal_acceptance_required).toBe(false);
		// No second /me: the endpoint's answer is the profile.
		expect(get).toHaveBeenCalledTimes(1);
	});

	it('a call the server-side gate refuses puts the step up on a loaded profile (§505)', async () => {
		const portalAuth = await freshStore();
		const { portalAcceptanceRefusal } = await import('#lib/api/acceptanceRefusal.ts');
		get.mockResolvedValueOnce({ ...SUPPLIER, legal_acceptance_required: false });
		await portalAuth.fetchUser();

		// What `portalApi.ts` does on any refused response.
		portalAcceptanceRefusal.notice(403, 'privacy_acknowledgement_required');

		expect(portalAuth.user?.legal_acceptance_required).toBe(true);
		expect(portalAuth.user?.vendor_id).toBe(SUPPLIER.vendor_id);
	});

	it('a refused acknowledgement is thrown for the step to state and leaves the step up', async () => {
		const portalAuth = await freshStore();
		get.mockResolvedValueOnce({ ...SUPPLIER, legal_acceptance_required: true });
		await portalAuth.fetchUser();

		const refusal = Object.assign(new Error('stale'), {
			status: 422,
			code: 'privacy_acknowledgement_required'
		});
		post.mockRejectedValueOnce(refusal);
		await expect(portalAuth.acknowledgeLegal()).rejects.toBe(refusal);
		expect(portalAuth.user?.legal_acceptance_required).toBe(true);
	});
});
