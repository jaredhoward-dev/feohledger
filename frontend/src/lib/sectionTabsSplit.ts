/**
 * Which section tabs stay in the row and which go into the More menu
 * (`components/layout/SectionTabs.svelte`).
 *
 * Pure so the arithmetic is testable without a layout engine. The one input
 * that has to be measured carefully is `barWidth`: it is the content width of
 * the container that holds BOTH the tab row and the More button
 * (`.section-tabs-inner`), never the row's own width. The row is a flex
 * sibling of More, so once More renders the row has already shrunk by More's
 * width; budgeting from the row and then subtracting More again took More out
 * twice and pushed about 80px of tabs into the menu that had room in the bar
 * (decisions §590).
 */

export interface SectionTabsInput {
	/** Each tab's intrinsic width, in tab order. */
	widths: readonly number[];
	/** Content width of the container holding the row and the More button. */
	barWidth: number;
	/** The More button's intrinsic width. */
	moreWidth: number;
	/** The flex `gap` between two tabs inside the row. */
	gap: number;
	/** The active tab's index, or -1. The active tab always stays in the row. */
	activeIndex: number;
}

export interface SectionTabsSplit {
	/** Indices of the tabs rendered in the row, in row order. */
	visible: number[];
	/** Indices of the tabs in the More menu, in tab order. */
	overflow: number[];
}

/** Width of `count` tabs laid out in the row, gaps included. */
function rowWidth(widths: readonly number[], gap: number): number {
	return widths.reduce((a, b) => a + b, 0) + gap * Math.max(0, widths.length - 1);
}

export function splitSectionTabs({
	widths,
	barWidth,
	moreWidth,
	gap,
	activeIndex
}: SectionTabsInput): SectionTabsSplit {
	const all = widths.map((_, i) => i);
	if (rowWidth(widths, gap) <= barWidth) return { visible: all, overflow: [] };

	// More sits beside the row with no gap between them (`.section-tabs-inner`
	// has none), so its width comes out of the bar exactly once.
	const budget = barWidth - moreWidth;
	let used = 0;
	let n = 0;
	for (const w of widths) {
		const step = w + (n > 0 ? gap : 0);
		if (used + step > budget) break;
		used += step;
		n++;
	}
	if (n < 1) n = 1; // never render an empty row

	let visible = all.slice(0, n);
	// The active tab always stays in the row: a section whose current page is
	// only reachable by opening a menu reads as "nothing is selected".
	if (activeIndex >= n) visible = [...all.slice(0, Math.max(0, n - 1)), activeIndex];
	const shown = new Set(visible);
	return { visible, overflow: all.filter((i) => !shown.has(i)) };
}
