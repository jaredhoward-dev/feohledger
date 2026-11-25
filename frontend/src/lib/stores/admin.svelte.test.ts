import { beforeEach, describe, expect, it, vi } from 'vitest';

// `adminStore.rolesLoaded` decides whether /admin?tab=roles draws DataTable's
// loading row or the settled table: until a roles load has settled the panel
// must not claim "No system roles." (the layout-stability spec measures the
// tab from that first paint). `#lib/api.ts` reaches `$app/env/public` through
// `#lib/tenant.ts`, which only SvelteKit resolves, so it is replaced with the
// one seam the roles path uses.
const get = vi.fn();

vi.mock('#lib/api.ts', () => ({
	api: { get: (...args: unknown[]) => get(...args) }
}));

async function freshStore() {
	vi.resetModules();
	const mod = await import('./admin.svelte.ts');
	return mod.adminStore;
}

beforeEach(() => {
	get.mockReset();
});

describe('adminStore roles load', () => {
	it('is not loaded until the first roles response settles', async () => {
		const store = await freshStore();
		let resolve!: (v: unknown) => void;
		get.mockReturnValueOnce(new Promise((r) => (resolve = r)));
		expect(store.rolesLoaded).toBe(false);
		const pending = store.fetchRoles();
		expect(store.rolesLoaded).toBe(false);
		resolve([{ id: 'r1', name: 'admin', description: null, is_system: true, permissions: [] }]);
		await pending;
		expect(store.rolesLoaded).toBe(true);
		expect(store.rolesErrored).toBe(false);
		expect(store.roles).toHaveLength(1);
	});

	it('counts a failed load as settled, with the error flag up', async () => {
		const store = await freshStore();
		get.mockRejectedValueOnce(new Error('boom'));
		await store.fetchRoles();
		expect(store.rolesLoaded).toBe(true);
		expect(store.rolesErrored).toBe(true);
	});
});
