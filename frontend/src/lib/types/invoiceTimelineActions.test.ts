import { describe, expect, it, vi } from 'vitest';

// The store reaches `$app/*`; nothing below reads a message.
vi.mock('#lib/i18n/store.svelte.ts', () => ({
	hasMessage: () => false
}));

const { activityTimelineView } = await import('./invoiceTimelineActions');

const settled = { fetched: true, errored: false };

// decisions §529 / §534: the rows are stored the moment their fetch returns
// (the ERP panel reads them), but the Activity list must not draw one before
// the label slice lands, or it renders as a raw token (`invoice.erp_failed`)
// and relabels.
describe('activityTimelineView', () => {
	it('draws no row before the label slice has landed, whatever the fetch did', () => {
		for (const fetch of [
			{ fetched: false, errored: false, rowCount: 0 },
			{ fetched: true, errored: false, rowCount: 3 },
			{ fetched: true, errored: true, rowCount: 0 },
			{ fetched: true, errored: false, rowCount: 0 }
		]) {
			expect(activityTimelineView({ labelsReady: false, ...fetch })).toBe('loading');
		}
	});

	it('draws the rows once the labels are in', () => {
		expect(activityTimelineView({ labelsReady: true, ...settled, rowCount: 2 })).toBe('rows');
	});

	it('keeps rows it already holds up through a refetch or a failed one', () => {
		expect(
			activityTimelineView({ labelsReady: true, fetched: false, errored: false, rowCount: 2 })
		).toBe('rows');
		expect(
			activityTimelineView({ labelsReady: true, fetched: true, errored: true, rowCount: 2 })
		).toBe('rows');
	});

	it('waits for the first fetch before claiming the list is empty', () => {
		expect(
			activityTimelineView({ labelsReady: true, fetched: false, errored: false, rowCount: 0 })
		).toBe('loading');
	});

	it('tells a failed load apart from an invoice with no activity', () => {
		expect(
			activityTimelineView({ labelsReady: true, fetched: true, errored: true, rowCount: 0 })
		).toBe('errored');
		expect(activityTimelineView({ labelsReady: true, ...settled, rowCount: 0 })).toBe('empty');
	});
});
