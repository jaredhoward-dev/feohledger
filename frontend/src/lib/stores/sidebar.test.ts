import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SHELL_NARROW_QUERY } from '#lib/breakpoints.ts';
import { createSidebarStore, sidebar } from './sidebar.svelte.ts';

/**
 * The shell sidebar's state (decisions §590, #635 finding 39). At or below the
 * narrow breakpoint it is the icon rail unless the reader opens the panel,
 * which then floats as an overlay; on a wide viewport it is whatever the
 * reader last chose, and a trip through narrow does not overwrite that. The
 * store is a module singleton, so each test puts it back to wide + expanded.
 */
describe('sidebar store', () => {
	beforeEach(() => {
		sidebar.setNarrow(false);
		if (sidebar.collapsed) sidebar.toggle();
	});

	it('starts wide and expanded, which is not an overlay', () => {
		expect(sidebar.collapsed).toBe(false);
		expect(sidebar.narrow).toBe(false);
		expect(sidebar.overlay).toBe(false);
	});

	it('auto-collapses to the icon rail when the viewport turns narrow', () => {
		sidebar.setNarrow(true);
		expect(sidebar.collapsed).toBe(true);
		expect(sidebar.overlay).toBe(false);
	});

	it('opening the panel on a narrow viewport is the overlay, and collapse closes it', () => {
		sidebar.setNarrow(true);
		sidebar.toggle();
		expect(sidebar.collapsed).toBe(false);
		expect(sidebar.overlay).toBe(true);

		sidebar.collapse();
		expect(sidebar.collapsed).toBe(true);
		expect(sidebar.overlay).toBe(false);
	});

	it('is never an overlay on a wide viewport', () => {
		sidebar.toggle();
		sidebar.toggle();
		expect(sidebar.overlay).toBe(false);
		sidebar.collapse();
		expect(sidebar.overlay).toBe(false);
	});

	it('restores the reader’s wide choice after a trip through narrow — expanded', () => {
		sidebar.setNarrow(true);
		expect(sidebar.collapsed).toBe(true);
		sidebar.setNarrow(false);
		expect(sidebar.collapsed).toBe(false);
	});

	it('restores the reader’s wide choice after a trip through narrow — collapsed', () => {
		sidebar.collapse();
		sidebar.setNarrow(true);
		// Opening and closing the overlay on the phone layout is a narrow-only
		// choice: it must not leak into the wide one.
		sidebar.toggle();
		expect(sidebar.overlay).toBe(true);
		sidebar.setNarrow(false);
		expect(sidebar.collapsed).toBe(true);
		expect(sidebar.overlay).toBe(false);
	});

	it('crossing into narrow closes an overlay left open from an earlier narrow spell', () => {
		sidebar.setNarrow(true);
		sidebar.toggle();
		sidebar.setNarrow(false);
		sidebar.setNarrow(true);
		expect(sidebar.overlay).toBe(false);
		expect(sidebar.collapsed).toBe(true);
	});

	it('a repeated setNarrow with the same value changes nothing', () => {
		sidebar.setNarrow(true);
		sidebar.toggle();
		sidebar.setNarrow(true);
		expect(sidebar.overlay).toBe(true);
	});

	it('collapse is idempotent, unlike toggle', () => {
		sidebar.collapse();
		sidebar.collapse();
		expect(sidebar.collapsed).toBe(true);
		sidebar.toggle();
		expect(sidebar.collapsed).toBe(false);
	});
});

/**
 * `narrow` is seeded from `matchMedia` when the store is created. Left to the
 * root layout's effect, which runs after the first paint, a phone drew the
 * 220px panel for a frame before collapsing to the rail.
 */
describe('sidebar store seeding', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	function stubViewport(narrow: boolean) {
		const matchMedia = vi.fn((query: string) => ({ matches: narrow, media: query }));
		vi.stubGlobal('window', { matchMedia });
		return matchMedia;
	}

	it('starts as the rail when the viewport is already narrow', () => {
		const matchMedia = stubViewport(true);
		const store = createSidebarStore();
		expect(matchMedia).toHaveBeenCalledWith(SHELL_NARROW_QUERY);
		expect(store.narrow).toBe(true);
		expect(store.collapsed).toBe(true);
		expect(store.overlay).toBe(false);
	});

	it('starts as the expanded panel when the viewport is wide', () => {
		stubViewport(false);
		const store = createSidebarStore();
		expect(store.narrow).toBe(false);
		expect(store.collapsed).toBe(false);
	});

	it('the layout\'s first setNarrow with the seeded value changes nothing', () => {
		stubViewport(true);
		const store = createSidebarStore();
		store.toggle();
		store.setNarrow(true);
		expect(store.overlay).toBe(true);
	});

	it('falls back to wide where there is no matchMedia', () => {
		vi.stubGlobal('window', {});
		expect(createSidebarStore().narrow).toBe(false);
		vi.stubGlobal('window', undefined);
		expect(createSidebarStore().narrow).toBe(false);
	});
});
