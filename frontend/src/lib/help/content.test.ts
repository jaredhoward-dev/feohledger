import { describe, expect, it } from 'vitest';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import { SLICE_LOADERS } from '#lib/i18n/catalogues.ts';
import { SUPPORTED_LOCALES } from '#lib/i18n/locale.ts';
import { NAV } from '#lib/nav.ts';
import { INVOICE_STATUSES } from '#lib/types/invoice.ts';
import { headingId } from './anchor.ts';
import {
	GLOSSARY,
	GLOSSARY_CATEGORIES,
	GUIDES,
	HELP_ROLES,
	PAGE_HELP,
	ROLE_START,
	guideFor,
	guidesForTerm,
	navPageFor,
	searchHelp,
	termFor,
	termKey,
	termShortKey,
	type Label
} from './content.ts';
import { inline } from './inline.ts';
import { LIFECYCLE } from './lifecycle.ts';
import { PORTAL_GLOSSARY, PORTAL_GUIDES, PORTAL_PAGES, portalGuideFor } from './portal.ts';
import { DIAGRAM_IDS } from './types.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

/**
 * The help centre's content is hand-written prose that points at the live app
 * — catalogue labels, routes, sidebar entries, workflow statuses — and prose
 * rots silently when the thing it describes is renamed. Every such reference is
 * resolved here, so a rename fails CI instead of leaving a guide that names a
 * button that no longer exists.
 */

// Every slice: guide prose names chrome keys (main catalogue), glossary /
// diagram keys (help slice, decisions §261) and the settings screens' controls
// (settings slice, §300 — which is why /help loads it).
const catalogue = EN_ALL as Record<string, string>;
const label: Label = (key: MessageKey) => catalogue[key] ?? key;

const ROUTES = import.meta.glob('/src/routes/**/+page.svelte');
const LIGHT_MODULES = import.meta.glob(['/src/lib/help/routeHelp.ts', '/src/lib/help/pages.ts', '/src/lib/help/terms.ts'], {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;
const SVELTE = import.meta.glob('/src/**/*.svelte', { query: '?raw', import: 'default', eager: true }) as Record<
	string,
	string
>;

/** The page file a link's path lands on (query and hash dropped). */
function routeExists(href: string): boolean {
	const path = href.split(/[?#]/)[0].replace(/\/$/, '');
	return `/src/routes${path}/+page.svelte` in ROUTES;
}

const NAV_HREFS = NAV.flatMap((e) => (e.kind === 'link' ? [e.href] : e.children.map((c) => c.href)));

/** Every piece of help prose, with where it lives, for the reference checks. */
function proseSources(): { where: string; text: string }[] {
	const out: { where: string; text: string }[] = [];
	for (const g of GUIDES) {
		out.push({ where: `guide ${g.id} summary`, text: g.summary });
		for (const s of g.sections) {
			out.push({ where: `guide ${g.id} heading`, text: s.heading });
			s.blocks.forEach((b, i) => {
				const where = `guide ${g.id} › ${s.heading} › block ${i}`;
				if (b.type === 'p' || b.type === 'note') out.push({ where, text: b.text });
				else if (b.type === 'steps' || b.type === 'list') b.items.forEach((t) => out.push({ where, text: t }));
				else if (b.type === 'diagram') out.push({ where, text: b.caption });
			});
		}
	}
	for (const t of GLOSSARY) out.push({ where: `glossary ${t.id}`, text: t.long });
	for (const [href, p] of Object.entries(PAGE_HELP)) out.push({ where: `page ${href}`, text: p.summary });
	for (const s of LIFECYCLE) out.push({ where: `lifecycle ${s.id}`, text: s.text });
	return out;
}

describe('help content — guides', () => {
	it('has unique, URL-safe ids', () => {
		const ids = GUIDES.map((g) => g.id);
		expect(new Set(ids).size).toBe(ids.length);
		for (const id of ids) expect(id, id).toMatch(/^[a-z0-9]+(-[a-z0-9]+)*$/);
	});

	it('every guide has a summary, sections, and anchors that do not collide', () => {
		for (const g of GUIDES) {
			expect(g.summary.length, `${g.id} summary`).toBeGreaterThan(0);
			expect(g.summary.length, `${g.id} summary length`).toBeLessThanOrEqual(220);
			expect(g.sections.length, `${g.id} sections`).toBeGreaterThan(0);
			const anchors = g.sections.map((s) => headingId(s.heading));
			expect(new Set(anchors).size, `${g.id} heading anchors`).toBe(anchors.length);
			for (const s of g.sections) expect(s.blocks.length, `${g.id} › ${s.heading}`).toBeGreaterThan(0);
		}
	});

	it("a guide's route is a real page and its terms and related guides exist", () => {
		for (const g of GUIDES) {
			if (g.route) expect(routeExists(g.route), `${g.id} route ${g.route}`).toBe(true);
			for (const t of g.terms ?? []) expect(termFor(t), `${g.id} term ${t}`).toBeDefined();
			for (const r of g.related ?? []) {
				expect(guideFor(r), `${g.id} related ${r}`).toBeDefined();
				expect(r, `${g.id} relates to itself`).not.toBe(g.id);
			}
		}
	});

	it('each role has a start guide, and every guide kind is shown somewhere', () => {
		for (const r of HELP_ROLES) expect(guideFor(ROLE_START[r])?.kind, r).toBe('start');
		expect(new Set(GUIDES.map((g) => g.kind))).toEqual(new Set(['start', 'howto', 'concept']));
	});

	it('the lifecycle walkthrough is in the invoice-lifecycle guide', () => {
		const blocks = guideFor('invoice-lifecycle')?.sections.flatMap((s) => s.blocks) ?? [];
		expect(blocks.some((b) => b.type === 'lifecycle')).toBe(true);
	});
});

describe('help content — every inline reference resolves', () => {
	const sources = proseSources();

	it('parses with no stray markup left in the text', () => {
		for (const { where, text } of sources) {
			for (const p of inline(text)) {
				if (p.kind !== 'text') continue;
				expect(p.text, `${where}: unparsed markup`).not.toMatch(/\[\[|\]\]|\{ui:|\*\*/);
			}
		}
	});

	it('{ui:key} names a catalogue label that takes no parameters', () => {
		for (const { where, text } of sources) {
			for (const p of inline(text)) {
				if (p.kind !== 'ui') continue;
				expect(catalogue[p.key], `${where}: {ui:${p.key}} is not in en.ts`).toBeDefined();
				// RichText renders the label with no params, so a placeholder would show literally.
				expect(catalogue[p.key], `${where}: {ui:${p.key}} has a placeholder`).not.toMatch(/\{[a-zA-Z]/);
			}
		}
	});

	it('[[term]], [[guide:…]] and [[page:…]] point at things that exist', () => {
		for (const { where, text } of sources) {
			for (const p of inline(text)) {
				if (p.kind === 'term') expect(termFor(p.id), `${where}: [[${p.id}]]`).toBeDefined();
				if (p.kind === 'guide') expect(guideFor(p.id), `${where}: [[guide:${p.id}]]`).toBeDefined();
				if (p.kind === 'page') {
					expect(routeExists(p.href), `${where}: [[page:${p.href}]] is not a route`).toBe(true);
					// Unlabelled links take the sidebar label, so they must be sidebar entries.
					if (!p.label) expect(navPageFor(p.href), `${where}: [[page:${p.href}]] needs a label`).toBeDefined();
				}
			}
		}
	});
});

describe('help content — glossary', () => {
	it('has unique ids in known categories, every one with a name and a one-liner in every locale', async () => {
		const ids = GLOSSARY.map((t) => t.id);
		expect(new Set(ids).size).toBe(ids.length);
		for (const t of GLOSSARY) expect(GLOSSARY_CATEGORIES, t.id).toContain(t.category);
		for (const loc of SUPPORTED_LOCALES) {
			const dict = (await SLICE_LOADERS.help[loc]()) as Record<string, string>;
			for (const id of ids) {
				expect(dict[termKey(id)], `${loc}: ${termKey(id)}`).toBeTruthy();
				const short = dict[termShortKey(id)];
				expect(short, `${loc}: ${termShortKey(id)}`).toBeTruthy();
				// It is a tooltip: one sentence that fits the bubble.
				expect(short.length, `${loc}: ${termShortKey(id)} is too long for a tip`).toBeLessThanOrEqual(160);
			}
		}
	});

	it('has no catalogue entry for a term that is not in the glossary', () => {
		const ids = new Set(GLOSSARY.map((t) => t.id));
		for (const key of Object.keys(catalogue)) {
			const m = key.match(/^help\.term\.([a-z0-9-]+?)(\.short)?$/);
			if (m) expect(ids.has(m[1]), `${key} has no glossary entry`).toBe(true);
		}
	});

	it('related entries exist', () => {
		for (const t of GLOSSARY) for (const r of t.related ?? []) expect(termFor(r), `${t.id} → ${r}`).toBeDefined();
	});

	/**
	 * The ids every `<HelpTip>` in the tree can show: a literal `term="…"`, or
	 * every quoted id a `term={…}` expression can evaluate to (the invoice modal
	 * picks two-, three- or four-way by the match type; the literals it compares
	 * against are skipped), and every `helpTerm="…"` handed to a component that
	 * forwards it to its own HelpTip (ApprovalChainProgress). An expression with no quoted
	 * id in it could show anything, so it fails.
	 */
	function tippedTerms(): { file: string; ids: string[] }[] {
		const out: { file: string; ids: string[] }[] = [];
		/** Ids an attribute value can evaluate to: a literal, or every quoted id in an expression that isn't a comparison operand. */
		const idsIn = (literal: string | undefined, expr: string | undefined) =>
			literal !== undefined
				? [literal]
				: [...(expr ?? '').matchAll(/(?<![=!]==?\s*)['"]([a-z0-9-]+)['"]/g)].map((m) => m[1]);
		// An opening tag, self-closing or not, whose attributes may hold one level of `{…}`.
		const TAG = /<HelpTip\b((?:[^>{}]|\{[^{}]*\})*)>/g;
		for (const [file, src] of Object.entries(SVELTE)) {
			for (const tag of src.matchAll(TAG)) {
				const literal = tag[1].match(/\bterm="([^"]+)"/)?.[1];
				const expr = tag[1].match(/\bterm=\{([^{}]*)\}/)?.[1];
				// A component that forwards a caller's id — KpiCard / ApprovalChainProgress's
				// `helpTerm` prop, DataTable's `column.help` — is checked at its callers below.
				if (expr && ['helpTerm', 'col.help'].includes(expr.trim())) continue;
				out.push({ file, ids: idsIn(literal, expr) });
			}
			for (const prop of src.matchAll(/\bhelpTerm=(?:"([^"]+)"|\{([^{}]*)\})/g))
				out.push({ file, ids: idsIn(prop[1], prop[2]) });
			for (const col of src.matchAll(/\bhelp:\s*(?:'([^']+)'|"([^"]+)")/g)) out.push({ file, ids: [col[1] ?? col[2]] });
		}
		return out;
	}

	it('every <HelpTip> in the app names glossary entries', () => {
		const uses = tippedTerms();
		for (const { file, ids } of uses) {
			expect(ids.length, `${file}: HelpTip term must be a literal id or an expression over literal ids`).toBeGreaterThan(0);
			for (const id of ids) expect(termFor(id), `${file}: HelpTip term "${id}"`).toBeDefined();
		}
		expect(uses.length).toBeGreaterThan(0);
	});

	it('every term a HelpTip can show has further reading', () => {
		const tipped = new Set(tippedTerms().flatMap((u) => u.ids));
		const without = [...tipped].filter((id) => guidesForTerm(id).length === 0);
		expect(without, 'tipped terms no guide lists in `terms`').toEqual([]);
	});
});

describe('help content — the page directory', () => {
	it('covers every sidebar destination, and nothing that is not a page', () => {
		for (const href of NAV_HREFS) expect(PAGE_HELP[href], `PAGE_HELP is missing ${href}`).toBeDefined();
		for (const href of Object.keys(PAGE_HELP)) expect(routeExists(href), `PAGE_HELP ${href}`).toBe(true);
	});

	it("names guides that exist", () => {
		for (const [href, p] of Object.entries(PAGE_HELP)) if (p.guide) expect(guideFor(p.guide), href).toBeDefined();
	});
});

describe('help content — lifecycle', () => {
	it('places every invoice status in exactly one stage, or as a branch off one', () => {
		const placed = LIFECYCLE.flatMap((s) => s.statuses);
		expect(new Set(placed).size, 'a status is in two stages').toBe(placed.length);
		const shown = new Set([...placed, ...LIFECYCLE.flatMap((s) => s.branches ?? [])]);
		expect([...shown].sort()).toEqual([...INVOICE_STATUSES].sort());
	});

	it('branches are real statuses and every stage has a guide', () => {
		for (const s of LIFECYCLE) {
			for (const b of s.branches ?? []) expect(INVOICE_STATUSES, `${s.id} → ${b}`).toContain(b);
			expect(guideFor(s.guide), `${s.id} guide`).toBeDefined();
		}
	});
});

describe('help content — diagrams', () => {
	const used = new Set(GUIDES.flatMap((g) => g.sections.flatMap((s) => s.blocks.flatMap((b) => (b.type === 'diagram' ? [b.id] : [])))));
	const dispatcher = SVELTE['/src/lib/components/help/diagrams/Diagram.svelte'] ?? '';

	it('every diagram is drawn and appears in at least one guide', () => {
		for (const id of DIAGRAM_IDS) {
			expect(used.has(id), `diagram ${id} is in no guide`).toBe(true);
			expect(dispatcher.includes(`'${id}'`), `Diagram.svelte does not draw ${id}`).toBe(true);
		}
	});
});

describe('supplier portal help — the same checks, inside the portal', () => {
	// The supplier guides (routes/portal/help/, decisions §508) are read by
	// vendor users who cannot open /help, so on top of the help centre's checks
	// every link they carry must stay inside the portal.
	const sources: { where: string; text: string }[] = [];
	for (const g of PORTAL_GUIDES) {
		sources.push({ where: `portal guide ${g.id} summary`, text: g.summary });
		for (const s of g.sections) {
			sources.push({ where: `portal guide ${g.id} heading`, text: s.heading });
			s.blocks.forEach((b, i) => {
				const where = `portal guide ${g.id} › ${s.heading} › block ${i}`;
				if (b.type === 'p' || b.type === 'note') sources.push({ where, text: b.text });
				else if (b.type === 'steps' || b.type === 'list') b.items.forEach((t) => sources.push({ where, text: t }));
				else if (b.type === 'diagram') sources.push({ where, text: b.caption });
			});
		}
	}

	it('are supplier guides, and no supplier guide is in the help centre', () => {
		expect(PORTAL_GUIDES.length).toBeGreaterThan(0);
		for (const g of PORTAL_GUIDES) expect(g.audience, g.id).toBe('supplier');
		for (const g of GUIDES) expect(g.audience ?? 'employee', g.id).toBe('employee');
		expect(PORTAL_GUIDES.filter((g) => g.kind === 'start')).toHaveLength(1);
	});

	it('have unique URL-safe ids, summaries, sections and distinct anchors', () => {
		const ids = PORTAL_GUIDES.map((g) => g.id);
		expect(new Set(ids).size).toBe(ids.length);
		for (const g of PORTAL_GUIDES) {
			expect(g.id).toMatch(/^[a-z0-9]+(-[a-z0-9]+)*$/);
			expect(g.summary.length, `${g.id} summary`).toBeGreaterThan(0);
			expect(g.summary.length, `${g.id} summary length`).toBeLessThanOrEqual(220);
			expect(g.sections.length, `${g.id} sections`).toBeGreaterThan(0);
			const anchors = g.sections.map((s) => headingId(s.heading));
			expect(new Set(anchors).size, `${g.id} heading anchors`).toBe(anchors.length);
			for (const s of g.sections) {
				expect(s.blocks.length, `${g.id} › ${s.heading}`).toBeGreaterThan(0);
				// Both blocks need employee-only pieces: the walkthrough imports the employee index.
				for (const b of s.blocks) expect(b.type, `${g.id} › ${s.heading}`).not.toBe('lifecycle');
			}
		}
	});

	it("a guide's route is a portal page, and its terms and related guides are the portal's own", () => {
		for (const route of Object.keys(PORTAL_PAGES)) expect(routeExists(route), route).toBe(true);
		for (const g of PORTAL_GUIDES) {
			if (g.route) expect(PORTAL_PAGES[g.route], `${g.id} route ${g.route}`).toBeDefined();
			for (const t of g.terms ?? []) expect(PORTAL_GLOSSARY, `${g.id} term ${t}`).toContain(t);
			for (const r of g.related ?? []) {
				expect(portalGuideFor(r), `${g.id} related ${r}`).toBeDefined();
				expect(r, `${g.id} relates to itself`).not.toBe(g.id);
			}
		}
	});

	it('every portal page a supplier works on has a guide that opens it', () => {
		// The notification settings are two checkboxes, covered by a tip in
		// "Track an invoice to payment" rather than a guide of their own.
		const covered = new Set(PORTAL_GUIDES.map((g) => g.route));
		const without = Object.keys(PORTAL_PAGES).filter((r) => r !== '/portal/notifications' && !covered.has(r));
		expect(without, 'portal pages no supplier guide opens').toEqual([]);
	});

	it('the glossary is help-centre terms (translated: the glossary checks above), each used by a guide', () => {
		expect(new Set(PORTAL_GLOSSARY).size).toBe(PORTAL_GLOSSARY.length);
		for (const id of PORTAL_GLOSSARY) {
			expect(termFor(id), id).toBeDefined();
			expect(catalogue[termKey(id)], id).toBeTruthy();
			expect(catalogue[termShortKey(id)], id).toBeTruthy();
		}
		// Every term on the page is one some supplier guide uses.
		const used = new Set(PORTAL_GUIDES.flatMap((g) => g.terms ?? []));
		for (const { text } of sources) for (const p of inline(text)) if (p.kind === 'term') used.add(p.id);
		expect(PORTAL_GLOSSARY.filter((id) => !used.has(id)), 'glossary terms no supplier guide uses').toEqual([]);
	});

	it('parses with no stray markup, and every {ui:key} is a label with no parameters', () => {
		for (const { where, text } of sources) {
			for (const p of inline(text)) {
				if (p.kind === 'text') expect(p.text, `${where}: unparsed markup`).not.toMatch(/\[\[|\]\]|\{ui:|\*\*/);
				if (p.kind !== 'ui') continue;
				expect(catalogue[p.key], `${where}: {ui:${p.key}} is not in en.ts`).toBeDefined();
				expect(catalogue[p.key], `${where}: {ui:${p.key}} has a placeholder`).not.toMatch(/\{[a-zA-Z]/);
			}
		}
	});

	it('every link stays inside the portal', () => {
		for (const { where, text } of sources) {
			for (const p of inline(text)) {
				if (p.kind === 'term') expect(PORTAL_GLOSSARY, `${where}: [[${p.id}]] is not on /portal/help`).toContain(p.id);
				if (p.kind === 'guide') expect(portalGuideFor(p.id), `${where}: [[guide:${p.id}]]`).toBeDefined();
				if (p.kind === 'page') {
					expect(p.href, `${where}: [[page:${p.href}]] leaves the portal`).toMatch(/^\/portal(\/|$)/);
					expect(routeExists(p.href), `${where}: [[page:${p.href}]] is not a route`).toBe(true);
					// The sidebar labels an unlabelled link, and the portal has no sidebar entry in NAV.
					expect(p.label, `${where}: [[page:${p.href}]] needs a label`).toBeDefined();
				}
			}
		}
	});

	it('the portal help pages render without the employee guide index', () => {
		// content.ts pulls every employee guide (and the settings-slice keys
		// they name) into whatever route imports it. The supplier pages, and
		// the shared renderers they use, reach help prose only through the
		// light modules.
		for (const file of [
			'/src/routes/portal/help/+page.svelte',
			'/src/routes/portal/help/[id]/+page.svelte',
			'/src/lib/components/help/GuideSections.svelte',
			'/src/lib/components/help/RichText.svelte',
			'/src/lib/components/help/HelpCrumbs.svelte',
			'/src/lib/components/help/EnglishNotice.svelte'
		]) {
			const src = SVELTE[file];
			expect(src, file).toBeDefined();
			expect(src, `${file} imports the employee help index`).not.toMatch(/#lib\/help\/(content|glossary|guides|lifecycle)|InvoiceLifecycle/);
		}
	});
});

describe('help content — bundle boundaries', () => {
	// A HelpTip and a page header render on ordinary pages: neither may pull the
	// help centre's prose (content.ts → every guide) into that page's chunk.
	it('HelpTip and PageHeader reach the guides only lazily', () => {
		const tip = SVELTE['/src/lib/components/help/HelpTip.svelte'];
		const header = SVELTE['/src/lib/components/ui/PageHeader.svelte'];
		expect(tip).not.toMatch(/^\s*import\s[^;]*['"]#lib\/help\/content\.ts['"]/m);
		expect(tip).toMatch(/import\(['"]#lib\/help\/content\.ts['"]\)/);
		expect(header).not.toMatch(/#lib\/help\/(content|glossary|guides)/);
	});

	// …and the light modules they DO import must stay light: one runtime import
	// of the content index from any of them would ship every guide everywhere.
	it('routeHelp, pages and terms import none of the prose', () => {
		expect(Object.keys(LIGHT_MODULES)).toHaveLength(3);
		for (const [name, src] of Object.entries(LIGHT_MODULES)) {
			const runtime = [...src.matchAll(/^\s*import\s+(?!type\b)[^;]*?from\s+['"]([^'"]+)['"]/gm)].map((m) => m[1]);
			for (const spec of runtime) expect(spec, `${name} imports ${spec}`).not.toMatch(/content|glossary|guides|lifecycle/);
		}
	});
});

describe('help search', () => {
	it('finds a guide by its title and a term by an alias', () => {
		const hits = searchHelp('payment run', label).slice(0, 3);
		expect(hits.some((h) => h.kind === 'guide' && h.guide.id === 'run-payments')).toBe(true);
		const sod = searchHelp('SoD', label);
		expect(sod.some((h) => h.kind === 'term' && h.term.id === 'segregation-of-duties')).toBe(true);
	});

	it('finds a page by its sidebar label', () => {
		expect(searchHelp('positive pay', label).some((h) => h.kind === 'page' && h.href === '/positive-pay')).toBe(true);
	});

	it('requires every word and returns nothing for an empty query', () => {
		expect(searchHelp('', label)).toEqual([]);
		expect(searchHelp('invoice zzqqxx', label)).toEqual([]);
	});

	it('matches a translated label, so a reader can search in their own language', () => {
		const german: Label = (key) => (key === termKey('three-way-match') ? 'Drei-Wege-Abgleich' : label(key));
		expect(searchHelp('Drei-Wege', german).some((h) => h.kind === 'term' && h.term.id === 'three-way-match')).toBe(
			true
		);
	});
});
