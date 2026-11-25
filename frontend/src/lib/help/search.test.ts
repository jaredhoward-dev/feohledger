import { describe, expect, it } from 'vitest';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';
import { GLOSSARY, GUIDES, PAGE_HELP, navPageFor, searchHelp } from './content.ts';
import { PORTAL_GLOSSARY, PORTAL_GUIDES } from './portal.ts';
import { searchPortalHelp } from './portalSearch.ts';
import { searchHelpIn, type Label } from './search.ts';
import { termKey } from './terms.ts';

/**
 * The help scorer (`search.ts`) over both of its callers' lists: the employee
 * help centre's (through `content.ts::searchHelp`) and the supplier help's
 * (`portalSearch.ts`). Plus the bundle boundary that made it its own module:
 * the supplier help page must never reach the employee guide index.
 */

const catalogue = EN_ALL as Record<string, string>;
const label: Label = (key: MessageKey) => catalogue[key] ?? key;

describe('search.ts — the scorer', () => {
	it('ranks a title match above a body match, and keeps source order on ties', () => {
		const guide = (id: string, title: string, summary: string) => ({
			id,
			title,
			summary,
			kind: 'howto' as const,
			sections: []
		});
		const guides = [
			guide('body', 'Something else', 'Mentions remittance in passing.'),
			guide('title', 'Remittance advice', 'What it is.'),
			guide('title-2', 'Remittance advice', 'The same title, listed later.')
		];
		const ids = searchHelpIn({ guides, terms: [] }, 'remittance', label).map((h) => (h.kind === 'guide' ? h.guide.id : ''));
		expect(ids).toEqual(['title', 'title-2', 'body']);
	});

	it('requires every word, ignores accents and case, and returns nothing for a blank query', () => {
		const guides = [{ id: 'g', title: 'Café payments', summary: '', kind: 'howto' as const, sections: [] }];
		expect(searchHelpIn({ guides, terms: [] }, 'CAFE', label)).toHaveLength(1);
		expect(searchHelpIn({ guides, terms: [] }, 'cafe zzqqxx', label)).toEqual([]);
		expect(searchHelpIn({ guides, terms: [] }, '   ', label)).toEqual([]);
	});

	it('labels an unlabelled [[page:…]] link through the caller’s pageLabel', () => {
		const guides = [
			{
				id: 'g',
				title: 'A guide',
				summary: 'Open [[page:/somewhere]] to start.',
				kind: 'howto' as const,
				sections: []
			}
		];
		const pageLabel = (href: string) => (href === '/somewhere' ? ('portal.nav.payments' as MessageKey) : undefined);
		expect(searchHelpIn({ guides, terms: [], pageLabel }, 'payments', label)).toHaveLength(1);
		expect(searchHelpIn({ guides, terms: [] }, 'payments', label)).toEqual([]);
	});
});

describe('employee help search (content.ts delegates to search.ts)', () => {
	it('is the shared scorer over GUIDES, GLOSSARY and PAGE_HELP', () => {
		const pageLabel = (href: string) => navPageFor(href)?.labelKey;
		for (const q of ['payment run', 'SoD', 'positive pay', 'vendor bank', 'three way']) {
			expect(searchHelp(q, label), q).toEqual(
				searchHelpIn({ guides: GUIDES, terms: GLOSSARY, pages: PAGE_HELP, pageLabel }, q, label)
			);
		}
	});

	it('still finds guides, aliases and pages', () => {
		expect(searchHelp('payment run', label).some((h) => h.kind === 'guide' && h.guide.id === 'run-payments')).toBe(true);
		expect(searchHelp('SoD', label).some((h) => h.kind === 'term' && h.term.id === 'segregation-of-duties')).toBe(true);
		expect(searchHelp('positive pay', label).some((h) => h.kind === 'page' && h.href === '/positive-pay')).toBe(true);
	});
});

describe('supplier help search (portalSearch.ts)', () => {
	it('finds a supplier guide by its title, best first', () => {
		const hits = searchPortalHelp('bank details', label);
		expect(hits[0]).toMatchObject({ kind: 'guide', guide: { id: 'portal-bank-details' } });
	});

	it('finds a portal glossary term by its catalogue name and one-liner', () => {
		const hits = searchPortalHelp('virtual card', label);
		expect(hits.some((h) => h.kind === 'term' && h.term.id === 'virtual-card')).toBe(true);
	});

	it('matches a translated term name, so a supplier can search in their language', () => {
		const german: Label = (key) => (key === termKey('virtual-card') ? 'Virtuelle Karte' : label(key));
		expect(searchPortalHelp('virtuelle', german).some((h) => h.kind === 'term' && h.term.id === 'virtual-card')).toBe(true);
	});

	it('only ever returns supplier guides and portal glossary terms', () => {
		const portalGuides = new Set(PORTAL_GUIDES.map((g) => g.id));
		const portalTerms = new Set(PORTAL_GLOSSARY);
		// Words that hit employee-only content too: an employee guide or term must never leak in.
		for (const q of ['invoice', 'payment', 'approval', 'vendor', 'segregation', 'payment run']) {
			for (const h of searchPortalHelp(q, label)) {
				if (h.kind === 'guide') expect(portalGuides.has(h.guide.id), `${q}: ${h.guide.id}`).toBe(true);
				else expect(portalTerms.has(h.term.id), `${q}: ${h.term.id}`).toBe(true);
			}
		}
		expect(searchPortalHelp('payment run', label).some((h) => h.kind === 'guide' && h.guide.id === 'run-payments')).toBe(false);
	});

	it('returns nothing for a blank query or a word no guide uses', () => {
		expect(searchPortalHelp('', label)).toEqual([]);
		expect(searchPortalHelp('zzqqxx', label)).toEqual([]);
	});
});

// ---------------------------------------------------------------------------
// The supplier help's import graph
// ---------------------------------------------------------------------------

const SOURCES = import.meta.glob(['/src/**/*.ts', '/src/**/*.svelte', '!/src/**/*.test.ts'], {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/** Static runtime imports and re-exports (`import type` / `export type` skipped: they erase). */
function runtimeSpecifiers(src: string): string[] {
	const re = /^\s*(?:import|export)\s+(?!type\b)(?:[^'";]*?\sfrom\s+)?['"]([^'"]+)['"]/gm;
	return [...src.matchAll(re)].map((mt) => mt[1]);
}

function resolve(from: string, spec: string): string | null {
	let base: string;
	if (spec.startsWith('#lib/')) base = `/src/lib/${spec.slice(5)}`;
	else if (spec.startsWith('.')) base = new URL(spec, `file://${from}`).pathname;
	else return null; // a package or `$app/…`: outside the app's own help modules
	for (const cand of [base, `${base}.ts`, `${base}/index.ts`]) if (cand in SOURCES) return cand;
	throw new Error(`${from}: cannot resolve ${spec}`);
}

function importGraph(entry: string): Set<string> {
	const seen = new Set<string>();
	const todo = [entry];
	while (todo.length) {
		const file = todo.pop()!;
		if (seen.has(file)) continue;
		seen.add(file);
		expect(SOURCES[file], file).toBeDefined();
		for (const spec of runtimeSpecifiers(SOURCES[file])) {
			const dep = resolve(file, spec);
			if (dep) todo.push(dep);
		}
	}
	return seen;
}

describe('supplier help — bundle boundary', () => {
	// content.ts joins every employee guide (and the settings-slice keys they
	// name). A static import anywhere under the supplier help page would ship
	// all of that to suppliers, so the whole graph is walked, not just the
	// page's own imports.
	const EMPLOYEE_INDEX = /^\/src\/lib\/help\/(content|glossary|lifecycle)\.ts$|^\/src\/lib\/help\/guides\//;

	for (const entry of ['/src/routes/portal/help/+page.svelte', '/src/routes/portal/help/[id]/+page.svelte']) {
		it(`${entry} never reaches the employee guide index`, () => {
			const graph = importGraph(entry);
			expect(graph.has('/src/lib/help/portal.ts')).toBe(true);
			const leaks = [...graph].filter((f) => EMPLOYEE_INDEX.test(f));
			expect(leaks, `${entry} reaches ${leaks.join(', ')}`).toEqual([]);
		});
	}

	it('the search page imports the scorer through portalSearch.ts', () => {
		expect(importGraph('/src/routes/portal/help/+page.svelte').has('/src/lib/help/search.ts')).toBe(true);
	});

	it('the walker catches a leak (sanity check on the employee search page)', () => {
		expect(importGraph('/src/routes/help/search/+page.svelte').has('/src/lib/help/content.ts')).toBe(true);
	});
});
