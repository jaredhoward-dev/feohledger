import { afterEach, describe, expect, it, vi } from 'vitest';

// The boot-path storage reads that used to blank the app when the browser
// blocks site storage (decisions §514, `docs/known-issues.md`): the employee
// token (`#lib/api.ts`), the supplier-portal token (`#lib/portalApi.ts`) and
// the entity selection (`#lib/entity.ts`). The auth stores call `hasToken()` /
// `hasPortalToken()` at module load, so a throw here was a throw out of the
// layout. Driven against the REAL modules with only their SvelteKit-only seams
// replaced (the `api/expenses.test.ts` arrangement).
vi.mock('#lib/tenant.ts', () => ({
	getApiBase: () => 'http://api.test',
	getTenantSlug: () => 'acme',
	getTenantStorageKey: () => 'acme'
}));
vi.mock('#lib/i18n/store.svelte.ts', () => ({
	m: (key: string) => key,
	localeReady: () => Promise.resolve()
}));

const { clearToken, getToken, hasToken, setToken } = await import('#lib/api.ts');
const { clearPortalToken, hasPortalToken, setPortalToken } = await import('#lib/portalApi.ts');
const { ALL_ENTITIES, getSelectedEntityId, getStoredEntitySelection, setStoredEntitySelection } =
	await import('#lib/entity.ts');

function blockedWindow() {
	const deny = () => {
		throw new DOMException('The operation is insecure.', 'SecurityError');
	};
	const win = {};
	Object.defineProperty(win, 'localStorage', { get: deny });
	Object.defineProperty(win, 'sessionStorage', { get: deny });
	return win;
}

function memoryWindow() {
	const data = new Map<string, string>();
	return {
		localStorage: {
			getItem: (k: string) => data.get(k) ?? null,
			setItem: (k: string, v: string) => void data.set(k, v),
			removeItem: (k: string) => void data.delete(k)
		}
	};
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('token + entity storage when the browser blocks site storage', () => {
	it('reads as signed out and never throws', () => {
		vi.stubGlobal('window', blockedWindow());
		expect(getToken()).toBeNull();
		expect(hasToken()).toBe(false);
		expect(hasPortalToken()).toBe(false);
		expect(() => setToken('tok')).not.toThrow();
		expect(() => clearToken()).not.toThrow();
		expect(() => setPortalToken('tok')).not.toThrow();
		expect(() => clearPortalToken()).not.toThrow();
		// A token that cannot be stored is not held anywhere else either.
		expect(hasToken()).toBe(false);
		expect(hasPortalToken()).toBe(false);
	});

	it('reads the entity selection as the consolidated default', () => {
		vi.stubGlobal('window', blockedWindow());
		expect(getStoredEntitySelection()).toBe(ALL_ENTITIES);
		expect(getSelectedEntityId()).toBeNull();
		expect(() => setStoredEntitySelection('entity-1')).not.toThrow();
		expect(() => setStoredEntitySelection(ALL_ENTITIES)).not.toThrow();
	});
});

describe('token + entity storage when storage works', () => {
	it('keeps the employee and portal tokens under separate keys', () => {
		const win = memoryWindow();
		vi.stubGlobal('window', win);
		setToken('employee');
		expect(getToken()).toBe('employee');
		expect(hasPortalToken()).toBe(false);
		setPortalToken('supplier');
		expect(win.localStorage.getItem('auth_token')).toBe('employee');
		expect(win.localStorage.getItem('portal_auth_token')).toBe('supplier');
		clearToken();
		expect(hasToken()).toBe(false);
		expect(hasPortalToken()).toBe(true);
		clearPortalToken();
		expect(hasPortalToken()).toBe(false);
	});

	it('persists a specific entity per tenant and clears it for the consolidated view', () => {
		const win = memoryWindow();
		vi.stubGlobal('window', win);
		setStoredEntitySelection('entity-1');
		expect(win.localStorage.getItem('selected_entity_id:acme')).toBe('entity-1');
		expect(getSelectedEntityId()).toBe('entity-1');
		setStoredEntitySelection(ALL_ENTITIES);
		expect(win.localStorage.getItem('selected_entity_id:acme')).toBeNull();
		expect(getSelectedEntityId()).toBeNull();
	});
});
