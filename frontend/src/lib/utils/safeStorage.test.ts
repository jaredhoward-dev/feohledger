import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	storageArea,
	storageAvailable,
	storageGet,
	storageRemove,
	storageSet
} from '#lib/utils/safeStorage.ts';

// The guarded storage door (decisions §514). A browser that blocks site data
// throws a `SecurityError` on the bare `window.localStorage` READ, so these
// install exactly that: a window whose storage properties are throwing getters.

function memoryStorage(): Storage {
	const data = new Map<string, string>();
	return {
		get length() {
			return data.size;
		},
		clear: () => data.clear(),
		key: (i: number) => [...data.keys()][i] ?? null,
		getItem: (k: string) => data.get(k) ?? null,
		setItem: (k: string, v: string) => void data.set(k, String(v)),
		removeItem: (k: string) => void data.delete(k)
	};
}

function blockedWindow() {
	const deny = () => {
		throw new DOMException('The operation is insecure.', 'SecurityError');
	};
	const win = {};
	Object.defineProperty(win, 'localStorage', { get: deny });
	Object.defineProperty(win, 'sessionStorage', { get: deny });
	return win;
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('safeStorage with storage blocked (the getter throws)', () => {
	it('reads as empty, drops writes and removes, and reports unavailable', () => {
		vi.stubGlobal('window', blockedWindow());
		for (const area of ['local', 'session'] as const) {
			expect(storageArea(area)).toBeNull();
			expect(storageGet('auth_token', area)).toBeNull();
			expect(storageSet('auth_token', 'tok', area)).toBe(false);
			expect(() => storageRemove('auth_token', area)).not.toThrow();
			expect(storageAvailable(area)).toBe(false);
		}
	});
});

describe('safeStorage with no window (prerender, node)', () => {
	it('behaves as blocked without touching any global', () => {
		vi.stubGlobal('window', undefined);
		expect(storageArea()).toBeNull();
		expect(storageGet('k')).toBeNull();
		expect(storageSet('k', 'v')).toBe(false);
		expect(storageAvailable()).toBe(false);
	});
});

describe('safeStorage with working storage', () => {
	it('round-trips through the area asked for, and only that one', () => {
		const local = memoryStorage();
		const session = memoryStorage();
		vi.stubGlobal('window', { localStorage: local, sessionStorage: session });

		expect(storageAvailable()).toBe(true);
		expect(storageSet('k', 'v')).toBe(true);
		expect(storageGet('k')).toBe('v');
		expect(storageGet('k', 'session')).toBeNull();
		expect(storageSet('k', 's', 'session')).toBe(true);
		expect(session.getItem('k')).toBe('s');

		storageRemove('k');
		expect(storageGet('k')).toBeNull();
		expect(storageGet('k', 'session')).toBe('s');
	});

	it('reports false when a write throws (quota) instead of throwing', () => {
		const store = memoryStorage();
		store.setItem = () => {
			throw new DOMException('quota', 'QuotaExceededError');
		};
		vi.stubGlobal('window', { localStorage: store });
		expect(storageSet('k', 'v')).toBe(false);
	});

	it('reads as empty when getItem itself throws', () => {
		const store = memoryStorage();
		store.getItem = () => {
			throw new DOMException('denied', 'SecurityError');
		};
		vi.stubGlobal('window', { localStorage: store });
		expect(storageGet('k')).toBeNull();
		expect(storageAvailable()).toBe(false);
	});
});

describe('every storage access goes through safeStorage', () => {
	// One unguarded read at boot blanked the whole app (decisions §514), and a
	// bare `localStorage.getItem` is an ordinary line to write. Code — not
	// comments — that touches either store outside this helper fails here.
	// `app.html`'s pre-paint script runs before any bundle, cannot import it,
	// and wraps its own reads.
	const SRC = new URL('../../', import.meta.url).pathname;
	const HELPER = 'lib/utils/safeStorage.ts';
	const ACCESS =
		/\b(?:window\.)?(?:localStorage|sessionStorage)\s*\.|typeof\s+(?:localStorage|sessionStorage)\b|=\s*(?:window\.)?(?:localStorage|sessionStorage)\b/;

	function sources(dir: string, acc: string[] = []): string[] {
		for (const name of readdirSync(dir)) {
			const full = join(dir, name);
			if (statSync(full).isDirectory()) sources(full, acc);
			else if (/\.(ts|svelte)$/.test(name) && !/\.test\.ts$/.test(name)) acc.push(full);
		}
		return acc;
	}

	it('finds no bare localStorage / sessionStorage outside the helper', () => {
		const offenders: string[] = [];
		for (const file of sources(SRC)) {
			const rel = file.slice(SRC.length);
			if (rel === HELPER) continue;
			readFileSync(file, 'utf8')
				.split('\n')
				.forEach((line, i) => {
					const code = line.replace(/\/\/.*$/, '').trim();
					if (code.startsWith('*') || code.startsWith('/*')) return;
					if (ACCESS.test(code)) offenders.push(`${rel}:${i + 1}: ${code}`);
				});
		}
		expect(offenders, 'use #lib/utils/safeStorage.ts instead').toEqual([]);
	});
});
