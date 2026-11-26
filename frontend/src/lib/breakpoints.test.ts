import { describe, expect, it } from 'vitest';
import { BREAKPOINT_VALUES, BREAKPOINTS, SHELL_NARROW_QUERY, maxWidth, minWidth } from './breakpoints';

/**
 * Static guard: every viewport width query in `src/` is on the breakpoint scale.
 *
 * #432: 42 width queries across 23 values had grown up one route at a time, and
 * `768`, `760`, `720` and `700` were four answers to the same question. The
 * scale (`breakpoints.ts`) collapsed them; this keeps a 24th from arriving. A
 * new layout picks the nearest scale value; a genuinely new step is a change to
 * the scale, its docs (`frontend/docs/ui-patterns.md` § Viewport floor and
 * breakpoints) and this guard's expectations together, not an inline number.
 *
 * `@container` queries are out of scope on purpose: they measure a component's
 * own box, not the viewport, so a shared viewport scale says nothing about them.
 */

const SOURCES = {
	...import.meta.glob('/src/**/*.svelte', { query: '?raw', import: 'default', eager: true }),
	...import.meta.glob('/src/**/*.css', { query: '?raw', import: 'default', eager: true })
} as Record<string, string>;

const files = Object.entries(SOURCES)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

interface WidthQuery {
	feature: 'min-width' | 'max-width';
	value: string;
	line: number;
}

/** Every `min-width` / `max-width` inside an `@media` prelude, comments stripped. */
function mediaWidths(source: string): WidthQuery[] {
	// Blank comments out (keeping newlines) so prose about an old value is not
	// read as a query and line numbers still point at the right place.
	const clean = source.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '));
	const out: WidthQuery[] = [];
	for (const media of clean.matchAll(/@media\b([^{;]*)\{/g)) {
		const prelude = media[1];
		const line = clean.slice(0, media.index).split('\n').length;
		for (const q of prelude.matchAll(/\((min-width|max-width)\s*:\s*([^)]+)\)/g)) {
			out.push({ feature: q[1] as WidthQuery['feature'], value: q[2].trim(), line });
		}
	}
	return out;
}

/** True when the value is one of the scale's px widths, written exactly. */
function onScale(value: string): boolean {
	const m = /^(\d+)px$/.exec(value);
	return !!m && BREAKPOINT_VALUES.includes(Number(m[1]));
}

describe('the breakpoint scale', () => {
	it('is ascending and has no duplicates', () => {
		const values = Object.values(BREAKPOINTS);
		expect(values).toEqual([...values].sort((a, b) => a - b));
		expect(new Set(values).size).toBe(values.length);
	});

	it('builds the queries the JS callers share', () => {
		expect(maxWidth('lg')).toBe('(max-width: 768px)');
		expect(minWidth('2xl')).toBe('(min-width: 1200px)');
		expect(SHELL_NARROW_QUERY).toBe('(max-width: 768px)');
	});
});

describe('every @media width query is on the scale', () => {
	it('finds the queries it is guarding (the scan is not empty)', () => {
		// A glob that silently matched nothing would make the next test pass.
		const all = files.flatMap((f) => mediaWidths(f.source));
		expect(all.length).toBeGreaterThan(20);
	});

	it.each(files.filter((f) => mediaWidths(f.source).length > 0).map((f) => [f.path, f.source]))(
		'%s',
		(path, source) => {
			const off = mediaWidths(source)
				.filter((q) => !onScale(q.value))
				.map((q) => `${path}:${q.line} (${q.feature}: ${q.value})`);
			expect(off, `not on the scale ${BREAKPOINT_VALUES.join('/')}px — see breakpoints.ts`).toEqual(
				[]
			);
		}
	);
});

/**
 * The boundary rule (decisions §590): where a wide and a narrow query meet at
 * the same scale value, the narrow side is written `not all and (min-width: N)`
 * so exactly one side matches at N. `(max-width: N)` beside `(min-width: N)`
 * matches both at N — the legal sheet was stripped of its card AND given the
 * wide gutter at exactly 1024px. Checked per file, which is where a pair meets.
 */
function boundaryClashes(queries: WidthQuery[]): WidthQuery[] {
	const mins = new Set(queries.filter((q) => q.feature === 'min-width').map((q) => q.value));
	return queries.filter((q) => q.feature === 'max-width' && mins.has(q.value));
}

describe('a max-width never meets a min-width at the same value', () => {
	it.each(files.filter((f) => mediaWidths(f.source).length > 0).map((f) => [f.path, f.source]))(
		'%s',
		(path, source) => {
			const clashes = boundaryClashes(mediaWidths(source)).map(
				(q) => `${path}:${q.line} (max-width: ${q.value})`
			);
			expect(
				clashes,
				'both sides match at this width — write the narrow side `not all and (min-width: N)`'
			).toEqual([]);
		}
	);
});

describe('the guard itself', () => {
	it('reads min and max widths out of a prelude, including compound ones', () => {
		expect(
			mediaWidths('@media (min-width: 480px) and (max-width: 767px) { a { b: c } }').map(
				(q) => `${q.feature}:${q.value}`
			)
		).toEqual(['min-width:480px', 'max-width:767px']);
	});

	it('reads the negated "below" form', () => {
		expect(mediaWidths('@media not all and (min-width: 360px) { a {} }')).toEqual([
			{ feature: 'min-width', value: '360px', line: 1 }
		]);
	});

	it('ignores comments, container queries and non-width media', () => {
		const css = `/* @media (max-width: 701px) { } */
@container help-main (max-width: 40rem) { a {} }
@media (prefers-reduced-motion: reduce) { a {} }
a { max-width: 333px; }`;
		expect(mediaWidths(css)).toEqual([]);
	});

	it('rejects an off-scale px value, a rem value, and a near miss', () => {
		expect(onScale('768px')).toBe(true);
		expect(onScale('700px')).toBe(false);
		expect(onScale('48rem')).toBe(false);
		expect(onScale('767px')).toBe(false);
	});

	it('flags a max-width that meets a min-width, and accepts the negated form', () => {
		const clash = `@media (max-width: 1024px) { a {} }
@media (min-width: 1024px) { b {} }`;
		expect(boundaryClashes(mediaWidths(clash))).toEqual([
			{ feature: 'max-width', value: '1024px', line: 1 }
		]);
		const ok = `@media not all and (min-width: 1024px) { a {} }
@media (min-width: 1024px) { b {} }
@media (max-width: 768px) { c {} }`;
		expect(boundaryClashes(mediaWidths(ok))).toEqual([]);
	});

	it('reports the line of the offending query', () => {
		expect(mediaWidths('a {}\n\n@media (max-width: 700px) {}')[0].line).toBe(3);
	});
});
