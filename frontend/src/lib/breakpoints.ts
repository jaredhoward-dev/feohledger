/**
 * The viewport breakpoint scale — the only widths a `@media` width query may use.
 *
 * CSS custom properties cannot appear inside a `@media` condition, and the
 * build has no `@custom-media` step (no PostCSS / Lightning CSS config), so a
 * `--bp-*` token would be decoration. The scale lives here instead, as data:
 * the JS `matchMedia` callers build their query from it, and
 * `breakpoints.test.ts` scans every `.svelte` / `.css` file under `src/` and
 * fails on a `min-width` / `max-width` value that is not one of these. The same
 * values are named in a comment at the top of `app.css` for whoever is writing
 * a stylesheet. Detail and the reasoning: `frontend/docs/ui-patterns.md`
 * § Viewport floor and breakpoints, `docs/decisions.md` §590.
 *
 * Pixels, not rems: every query had been px apart from three rem ones, and one
 * unit is what lets a reader compare two queries at a glance.
 *
 * Where a wide and a narrow query meet at one value, the narrow side is
 * written `not all and (min-width: N)`, so exactly one side matches at N
 * (`breakpoints.test.ts` flags a `max-width: N` beside a `min-width: N`).
 *
 * The supported floor is 320px (WCAG 2.2 SC 1.4.10). It is not a breakpoint —
 * nothing switches there — it is the width every route must still reflow at
 * (`tests-e2e/a11y/reflow.spec.ts`).
 */
export const BREAKPOINTS = {
	/** Under-360 phones (320–359). Written `not all and (min-width: 360px)`. */
	'2xs': 360,
	/** Small phones: tighten chrome that just fits at 375–400. */
	xs: 400,
	/** Phones: single-column forms and dialogs. */
	sm: 480,
	/** Large phones / small tablets: form grids and button rows stack. */
	md: 640,
	/** The app shell's NARROW breakpoint: at and below it the sidebar is the icon
	 *  rail and the workspace gutter halves. */
	lg: 768,
	/** Laptops: two-pane layouts (settings rail, auth panel, help rail) start. */
	xl: 1024,
	/** Wide desktops: the widest multi-column layouts (legal contents rail). */
	'2xl': 1200
} as const;

export type Breakpoint = keyof typeof BREAKPOINTS;

/** Every allowed width, ascending. */
export const BREAKPOINT_VALUES: readonly number[] = Object.values(BREAKPOINTS).sort((a, b) => a - b);

/** `(max-width: Npx)` — at or below the breakpoint. */
export function maxWidth(bp: Breakpoint): string {
	return `(max-width: ${BREAKPOINTS[bp]}px)`;
}

/** `(min-width: Npx)` — at or above the breakpoint. */
export function minWidth(bp: Breakpoint): string {
	return `(min-width: ${BREAKPOINTS[bp]}px)`;
}

/** The app shell's narrow query: sidebar auto-collapses to its icon rail. */
export const SHELL_NARROW_QUERY = maxWidth('lg');
