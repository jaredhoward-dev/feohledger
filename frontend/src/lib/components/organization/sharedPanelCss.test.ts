import { describe, expect, it } from 'vitest';
import appCss from '../../../app.css?raw';
import { extractStyleBlocks, parseRules, stripCssComments } from '../../a11y/cssAudit';

/**
 * The `/organization` panels share one form layout, declared once in
 * `app.css` under `.org-panel` (decisions §530). Before that each panel
 * carried a scoped copy, and a copy drifted: one panel lost the ≤600px rule
 * that folds `.form-grid` to one column.
 *
 *   1. `app.css` declares each shared rule under `.org-panel`, including the
 *      phone rule inside its media query.
 *   2. No panel's `<style>` names a shared class: a local copy, or a local
 *      tweak that would need its own cascade reasoning, both start the drift
 *      again. (`OrgSettingPanel` is the one exception: its `.section-footer`
 *      is a different rule, and it does not wear `org-panel`.)
 *   3. Every panel whose markup uses a shared class wears `org-panel` on its
 *      root, or it would render without the rules.
 */

const SHARED = ['form-grid', 'full-width', 'section-footer'] as const;

/** Its own `.section-footer` recipe; deliberately outside `.org-panel`. */
const OWN_RECIPE = new Set(['OrgSettingPanel.svelte']);

const RAW = import.meta.glob('./*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const PANELS = Object.entries(RAW).map(([path, source]) => [path.replace(/^\.\//, ''), source] as const);

function markup(source: string): string {
	let prev: string;
	let text = source;
	do {
		prev = text;
		text = text.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '');
	} while (text !== prev);
	return text;
}

function usesClass(html: string, cls: string): boolean {
	const attr = new RegExp(`class="[^"]*(?<![\\w-])${cls}(?![\\w-])[^"]*"`);
	const directive = new RegExp(`class:${cls}[=\\s>{]`);
	return attr.test(html) || directive.test(html);
}

function classesIn(selector: string): string[] {
	return [...selector.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((m) => m[1]);
}

/** The body of `@media (max-width: 600px) { … }` blocks in a stylesheet. */
function phoneBlocks(css: string): string[] {
	const out: string[] = [];
	const clean = stripCssComments(css);
	const re = /@media\s*\(max-width:\s*600px\)\s*\{/g;
	for (let m = re.exec(clean); m; m = re.exec(clean)) {
		let depth = 1;
		let i = m.index + m[0].length;
		const start = i;
		while (i < clean.length && depth > 0) {
			if (clean[i] === '{') depth++;
			else if (clean[i] === '}') depth--;
			i++;
		}
		out.push(clean.slice(start, i - 1));
	}
	return out;
}

describe('organization panels share their form layout from app.css', () => {
	const appRules = parseRules(appCss);
	// The first rule for a selector is the base one; `parseRules` also returns
	// the media-gated copy, without its query.
	const decl = (selector: string) =>
		new Map(appRules.find((r) => r.selector === selector)?.declarations ?? []);

	it('app.css declares the shared rules under .org-panel', () => {
		const grid = decl('.org-panel .form-grid');
		expect(grid.get('display')).toBe('grid');
		expect(grid.get('grid-template-columns')).toBe('1fr 1fr');
		expect(decl('.org-panel .full-width').get('grid-column')).toBe('1 / -1');
		const footer = decl('.org-panel .section-footer');
		// The wrap is what keeps the Save/Test row inside 320px (WCAG 1.4.10).
		expect(footer.get('flex-wrap')).toBe('wrap');
		expect(footer.get('display')).toBe('flex');
	});

	it('app.css folds .form-grid to one column at phone width', () => {
		const phone = phoneBlocks(appCss).flatMap((b) => parseRules(b));
		const rule = phone.find((r) => r.selector === '.org-panel .form-grid');
		expect(rule, 'the ≤600px .org-panel .form-grid rule').toBeDefined();
		expect(new Map(rule!.declarations).get('grid-template-columns')).toBe('1fr');
	});

	it('app.css reaches the shared classes only under .org-panel', () => {
		const loose = appRules
			.filter((r) => classesIn(r.selector).some((c) => (SHARED as readonly string[]).includes(c)))
			.filter((r) => !r.selector.split(',').every((p) => p.trim().startsWith('.org-panel ')));
		expect(loose.map((r) => r.selector)).toEqual([]);
	});

	it('covers the panels that use the shared layout', () => {
		const users = PANELS.filter(([, src]) => SHARED.some((c) => usesClass(markup(src), c)));
		expect(users.length).toBeGreaterThanOrEqual(13);
	});

	it.each(PANELS.filter(([name]) => !OWN_RECIPE.has(name)))(
		'%s keeps no scoped copy of a shared rule',
		(name, source) => {
			const rules = extractStyleBlocks(name, source).flatMap((b) => parseRules(b.css));
			const copies = rules
				.filter((r) => classesIn(r.selector).some((c) => (SHARED as readonly string[]).includes(c)))
				.map((r) => r.selector);
			expect(copies).toEqual([]);
		}
	);

	it.each(PANELS.filter(([name]) => !OWN_RECIPE.has(name)))(
		'%s wears org-panel if its markup uses the shared layout',
		(name, source) => {
			const html = markup(source);
			if (!SHARED.some((c) => usesClass(html, c))) return;
			expect(html).toMatch(/<section class="card org-panel"/);
		}
	);

	it('the guard reads a whole class name and a class: directive', () => {
		expect(usesClass('<div class="form-grid-x">', 'form-grid')).toBe(false);
		expect(usesClass('<div class="a form-grid">', 'form-grid')).toBe(true);
		expect(usesClass('<label class:full-width={x}>', 'full-width')).toBe(true);
		expect(phoneBlocks('@media (max-width: 600px) { .a { b: c; } }')).toEqual([' .a { b: c; } ']);
	});
});
