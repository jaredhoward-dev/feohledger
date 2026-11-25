import { describe, expect, it, vi } from 'vitest';

// The store reaches `$app/*`; only the seam's default slice loader touches it,
// and every case below passes its own.
vi.mock('#lib/i18n/store.svelte.ts', () => ({
	hasMessage: () => false,
	ensureSlices: () => Promise.resolve()
}));

const { loadLabelledTimeline } = await import('./invoiceTimelineActions');

/** A promise plus the handles to settle it from the test. */
function deferred<T>() {
	let resolve!: (v: T) => void;
	let reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

// decisions §529: the Activity rows must not reach the view before the label
// slice does, or they render as raw tokens (`invoice.erp_failed`) and relabel.
describe('loadLabelledTimeline', () => {
	it('holds the rows until the label slice has landed', async () => {
		const rows = deferred<string[]>();
		const labels = deferred<void>();
		let settled: string[] | undefined;
		const p = loadLabelledTimeline(() => rows.promise, () => labels.promise).then((r) => (settled = r));

		rows.resolve(['invoice.erp_failed']);
		await Promise.resolve();
		await Promise.resolve();
		expect(settled).toBeUndefined();

		labels.resolve();
		await p;
		expect(settled).toEqual(['invoice.erp_failed']);
	});

	it('holds the rows when the labels land first, too', async () => {
		const rows = deferred<number[]>();
		let settled: number[] | undefined;
		const p = loadLabelledTimeline(() => rows.promise, () => Promise.resolve()).then((r) => (settled = r));
		await Promise.resolve();
		expect(settled).toBeUndefined();
		rows.resolve([1]);
		await p;
		expect(settled).toEqual([1]);
	});

	it('rejects when the rows fetch does, so the caller keeps its fallback', async () => {
		await expect(
			loadLabelledTimeline(() => Promise.reject(new Error('boom')), () => Promise.resolve())
		).rejects.toThrow('boom');
	});
});
