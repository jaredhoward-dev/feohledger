/**
 * The one guarded door to `localStorage` / `sessionStorage`.
 *
 * Where a browser blocks site data for the origin (a "block all cookies"
 * setting, some private modes, a sandboxed frame), merely READING
 * `window.localStorage` throws a `SecurityError` — the property access, not
 * just `getItem`. An unguarded read reached at module load or during layout
 * boot (the auth store's `$state(hasToken())`) used to throw out of the
 * render and leave a blank page (`docs/known-issues.md`, decisions §514).
 *
 * So every storage access in the app goes through these helpers: an
 * unavailable store reads as empty (`null`), and a write or remove that cannot
 * land is dropped. Callers that need to know (the sign-in page's notice) ask
 * {@link storageAvailable}. Pure — no `$app/*` import — so vitest reaches it.
 */

export type StorageArea = 'local' | 'session';

/** The part of `Storage` the helpers use — narrower, so tests can fake it. */
export type KeyValueStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/**
 * `window.localStorage` / `window.sessionStorage`, or `null` where there is no
 * window or touching the property throws.
 */
export function storageArea(area: StorageArea = 'local'): Storage | null {
	try {
		if (typeof window === 'undefined') return null;
		return area === 'local' ? window.localStorage : window.sessionStorage;
	} catch {
		return null;
	}
}

/** `getItem`, or `null` when the store is unavailable or the read throws. */
export function storageGet(key: string, area: StorageArea = 'local'): string | null {
	try {
		return storageArea(area)?.getItem(key) ?? null;
	} catch {
		return null;
	}
}

/** `setItem`; `false` when it could not be stored (blocked, quota). */
export function storageSet(key: string, value: string, area: StorageArea = 'local'): boolean {
	const store = storageArea(area);
	if (!store) return false;
	try {
		store.setItem(key, value);
		return true;
	} catch {
		return false;
	}
}

/** `removeItem`; a store that cannot be reached holds nothing to remove. */
export function storageRemove(key: string, area: StorageArea = 'local'): void {
	try {
		storageArea(area)?.removeItem(key);
	} catch {
		// Nothing was stored there to remove.
	}
}

/**
 * Whether this browser lets the app reach the store at all. Signing in needs
 * it: the session token lives in `localStorage`, so without it a sign-in
 * cannot outlast the page it happened on. Deliberately no write probe — a
 * probe key would be one more thing stored in the visitor's browser, and the
 * Cookie Notice lists every one (`src/lib/legal/storageInventory.test.ts`).
 */
export function storageAvailable(area: StorageArea = 'local'): boolean {
	const store = storageArea(area);
	if (!store) return false;
	try {
		store.getItem('');
		return true;
	} catch {
		return false;
	}
}
