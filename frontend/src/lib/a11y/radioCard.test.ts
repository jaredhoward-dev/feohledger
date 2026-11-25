import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';
import { contrastRatio, WCAG_AA_NORMAL } from './contrast';
import { PALETTE_THEMES, parsePalette, parseRules, resolveColorValue } from './cssAudit';

/**
 * Static guard: a radio choice inside a dialog is drawn with the shared
 * `.radio-card` class (`app.css`), and that class keeps its promises.
 *
 * `app.css`'s `.modal label` turns a label into a stacked form field — column
 * flex, every direct-child `<span>` a small uppercase muted caption. A
 * `<label>` wrapping a radio picked that up and rendered as a centred column
 * of shouting captions: the `/billing` plan picker in production (PR #751),
 * then the GL-account ERP link, inspection result, statement intake mode,
 * webhook overlap and discount tier dialogs, each with its own partial
 * override (decisions §516).
 *
 * The three checks:
 *   1. every `<label>` with a direct `<input type="radio">` child inside a
 *      `<Modal>` (or anywhere in a `*Modal.svelte` body) carries `radio-card`;
 *   2. the stacked-field rule in `app.css` skips `.radio-card` and any label
 *      that wraps its own checkbox or radio;
 *   3. `.radio-card` is a row, sentence case, at least the 24px target
 *      (WCAG 2.5.8), marks selection and focus with `--accent`, and its text
 *      and rings clear WCAG 2.2 AA on `--surface` in both themes.
 */

const RAW = import.meta.glob('/src/**/*.{svelte,css}', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

const appCss = files.find((f) => f.path === 'app.css')?.source ?? '';

interface Node {
	type: string;
	name?: string;
	start: number;
	attributes?: { type: string; name: string; value: unknown }[];
	fragment?: { nodes: Node[] };
}

/** The static text of a `class="…"` attribute (expression parts dropped). */
function staticClass(el: Node): string {
	const attr = el.attributes?.find((a) => a.type === 'Attribute' && a.name === 'class');
	if (!attr || !Array.isArray(attr.value)) return '';
	return (attr.value as { type: string; data?: string }[])
		.filter((p) => p.type === 'Text')
		.map((p) => p.data ?? '')
		.join(' ');
}

function isRadioInput(node: Node): boolean {
	if (node.type !== 'RegularElement' || node.name !== 'input') return false;
	const type = node.attributes?.find((a) => a.type === 'Attribute' && a.name === 'type');
	return (
		Array.isArray(type?.value) &&
		(type.value as { type: string; data?: string }[]).some((p) => p.data === 'radio')
	);
}

/** Walk every child node (blocks included), tracking whether we are inside a `<Modal>`. */
function walk(node: unknown, inModal: boolean, visit: (n: Node, inModal: boolean) => void): void {
	if (node === null || typeof node !== 'object') return;
	if (Array.isArray(node)) {
		for (const child of node) walk(child, inModal, visit);
		return;
	}
	const record = node as Node & Record<string, unknown>;
	const modalHere = inModal || (record.type === 'Component' && record.name === 'Modal');
	if (typeof record.type === 'string') visit(record, modalHere);
	for (const [key, value] of Object.entries(record)) {
		if (key === 'parent' || key === 'attributes') continue;
		walk(value, modalHere, visit);
	}
}

/** `path:line  text` for each radio label in a dialog that lacks `radio-card`. */
function bareRadioLabels(path: string, source: string): string[] {
	// Cheap pre-check: the compiler parse is the cost of a tree scan.
	if (!/type=["']radio["']/.test(source)) return [];
	const fileIsModal = /Modal\.svelte$/.test(path);
	const ast = parse(source, { modern: true });
	const lines = source.split('\n');
	const out: string[] = [];
	walk(ast.fragment, fileIsModal, (el, inModal) => {
		if (!inModal || el.type !== 'RegularElement' || el.name !== 'label') return;
		if (!(el.fragment?.nodes ?? []).some(isRadioInput)) return;
		if (staticClass(el).split(/\s+/).includes('radio-card')) return;
		const line = source.slice(0, el.start).split('\n').length;
		out.push(`${path}:${line}  ${lines[line - 1].trim()}`);
	});
	return out;
}

const rules = parseRules(appCss);
const ruleFor = (selector: string) => rules.find((r) => r.selector === selector);
const decl = (selector: string, prop: string) =>
	ruleFor(selector)?.declarations.find(([name]) => name === prop)?.[1];

describe('radio cards in dialogs (decisions §516)', () => {
	it('the scanner flags a bare radio label in a dialog and accepts the class', () => {
		const bare = `<Modal open><label class="option"><input type="radio" name="x" /><span>A</span></label></Modal>`;
		const card = `<Modal open><label class="radio-card"><input type="radio" name="x" /><span>A</span></label></Modal>`;
		const outside = `<label class="option"><input type="radio" name="x" /><span>A</span></label>`;
		const inEach = `<Modal open>{#each xs as x (x)}<label><input type="radio" name="x" /></label>{/each}</Modal>`;
		expect(bareRadioLabels('t.svelte', bare)).toHaveLength(1);
		expect(bareRadioLabels('t.svelte', inEach)).toHaveLength(1);
		expect(bareRadioLabels('t.svelte', card)).toEqual([]);
		expect(bareRadioLabels('t.svelte', outside)).toEqual([]);
		// A `*Modal.svelte` body is a dialog even where `<Modal>` is out of view.
		expect(bareRadioLabels('XModal.svelte', outside)).toHaveLength(1);
	});

	it('every radio label inside a dialog is a .radio-card', () => {
		const offenders = files
			.filter((f) => f.path.endsWith('.svelte'))
			.flatMap(({ path, source }) => bareRadioLabels(path, source));
		expect(offenders, 'add class="radio-card" (app.css) to the radio\'s <label>').toEqual([]);
	});

	it('the stacked-field label rule skips radio cards and control-first labels', () => {
		const stacked = rules.filter(
			(r) => /^\.modal label\b/.test(r.selector) && r.selector.includes(':where(:not(.radio-card,')
		);
		expect(stacked.map((r) => r.selector.replace(/^.*\)\)\)/, '').trim())).toEqual(['', '> span']);
		for (const rule of stacked) {
			expect(rule.selector).toContain(':where(:not(.radio-card,');
			expect(rule.selector).toContain("> input[type='checkbox']");
			expect(rule.selector).toContain("> input[type='radio']");
		}
		// The control-first label is a row, not dropped to an inline box.
		const row = rules.find((r) =>
			r.selector.startsWith(".modal label:where(:not(.radio-card):has(> input[type='checkbox']")
		);
		expect(row?.declarations).toEqual(
			expect.arrayContaining([
				['display', 'flex'],
				['flex-direction', 'row']
			])
		);
		// No other `.modal label` rule may restyle every label in a dialog.
		const unscoped = rules.filter((r) => /^\.modal label( |>|$)/.test(r.selector));
		expect(unscoped.map((r) => r.selector)).toEqual([]);
	});

	it('.radio-card is a sentence-case row with a 24px target', () => {
		expect(decl('.radio-card', 'display')).toBe('flex');
		expect(decl('.radio-card', 'flex-direction')).toBe('row');
		expect(decl('.radio-card', 'text-transform')).toBe('none');
		expect(decl('.radio-card', 'letter-spacing')).toBe('normal');
		expect(parseFloat(decl('.radio-card', 'min-height') ?? '0')).toBeGreaterThanOrEqual(24);
		expect(decl('.radio-card:has(input:checked)', 'border-color')).toBe('var(--accent)');
		expect(decl('.radio-card:has(input:focus-visible)', 'outline')).toBe(
			'2px solid var(--accent)'
		);
		// Disabled is a colour, never opacity (which drops text under its floor).
		expect(decl('.radio-card:has(input:disabled)', 'color')).toBe('var(--text-muted)');
		expect(decl('.radio-card:has(input:disabled)', 'opacity')).toBeUndefined();
	});

	for (const theme of PALETTE_THEMES) {
		it(`.radio-card clears WCAG 2.2 AA on --surface (${theme})`, () => {
			const palette = parsePalette(appCss, theme);
			const surface = resolveColorValue('var(--surface)', palette);
			expect(surface).not.toBeNull();
			const ratio = (token: string) => contrastRatio(resolveColorValue(token, palette)!, surface!);
			// Text (1.4.3): the card's own colour and its disabled colour.
			expect(ratio('var(--text)')).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
			expect(ratio('var(--text-muted)')).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
			// Selected ring and focus outline (1.4.11 non-text, 2.4.13-style 3:1).
			expect(ratio('var(--accent)')).toBeGreaterThanOrEqual(3);
		});
	}
});
