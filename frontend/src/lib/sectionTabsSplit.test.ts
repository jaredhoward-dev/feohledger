import { describe, expect, it } from 'vitest';
import { splitSectionTabs, type SectionTabsInput } from './sectionTabsSplit';

/**
 * The section-tab budget (decisions §590). `barWidth` is the container that
 * holds the row AND the More button, so More's width comes out of it once;
 * the round-51 version budgeted from the row (already shrunk by More) and
 * took More out again.
 */

const GAP = 4;

function split(over: Partial<SectionTabsInput>) {
	return splitSectionTabs({
		widths: [100, 100, 100, 100],
		barWidth: 300,
		moreWidth: 60,
		gap: GAP,
		activeIndex: 0,
		...over
	});
}

/** Width of the given tabs laid out in the row. */
function rowWidth(widths: number[], idx: number[]): number {
	return idx.reduce((a, i) => a + widths[i], 0) + GAP * Math.max(0, idx.length - 1);
}

describe('splitSectionTabs', () => {
	it('keeps every tab in the row when they all fit, with no More', () => {
		expect(split({ barWidth: 412 })).toEqual({ visible: [0, 1, 2, 3], overflow: [] });
	});

	it('subtracts More exactly once', () => {
		// 300 - 60 = 240 of room: two tabs (204) fit. Budgeting from a row
		// already shrunk by More (240) and subtracting More + gap again left
		// 176 — one tab.
		expect(split({})).toEqual({ visible: [0, 1], overflow: [2, 3] });
	});

	it.each([
		[[120, 80, 150, 90, 200, 60], 500, 70],
		[[96, 132, 88, 170, 110], 360, 64],
		[[140, 140, 140, 140, 140, 140, 140], 900, 72],
		[[60, 60, 60, 60, 60, 60, 60, 60, 60], 288, 58]
	])('visible tabs + More fill the bar to within one tab (%j in %i)', (widths, barWidth, moreWidth) => {
		const { visible, overflow } = split({ widths, barWidth, moreWidth });
		expect(overflow.length).toBeGreaterThan(0);
		const used = rowWidth(widths, visible) + moreWidth;
		// Never wider than the bar…
		expect(used).toBeLessThanOrEqual(barWidth);
		// …and the next tab really would not have fit.
		const next = widths[visible.length];
		expect(used + GAP + next).toBeGreaterThan(barWidth);
	});

	it('keeps the active tab in the row, swapping out the last that fit', () => {
		expect(split({ activeIndex: 3 })).toEqual({ visible: [0, 3], overflow: [1, 2] });
	});

	it('never renders an empty row, even when nothing fits beside More', () => {
		expect(split({ widths: [400, 400], barWidth: 300 })).toEqual({
			visible: [0],
			overflow: [1]
		});
		expect(split({ widths: [400, 400], barWidth: 300, activeIndex: 1 })).toEqual({
			visible: [1],
			overflow: [0]
		});
	});

	it('treats no active tab (-1) as nothing to pin', () => {
		expect(split({ activeIndex: -1 })).toEqual({ visible: [0, 1], overflow: [2, 3] });
	});
});
