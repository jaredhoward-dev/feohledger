import { describe, it, expect } from 'vitest';
import { selectableMatchingTotal } from './invoice';

// The "Select all N matching" figure must be what `GET /api/invoices/ids`
// (with `exclude_status` = the system-managed set) will select — not the
// list's `total`, which counts the disabled, system-managed rows too.
describe('selectableMatchingTotal', () => {
	const counts = { new: 25, ready_for_review: 4, paid: 10, done: 3, pending: 2 };

	it('drops the system-managed statuses from the unfiltered tally', () => {
		expect(selectableMatchingTotal(counts, [])).toBe(29);
	});

	it('narrows to the active status filter', () => {
		expect(selectableMatchingTotal(counts, ['new'])).toBe(25);
		expect(selectableMatchingTotal(counts, ['new', 'paid'])).toBe(25);
		expect(selectableMatchingTotal(counts, ['paid', 'done'])).toBe(0);
	});

	it('reads an active status with no tally as zero', () => {
		expect(selectableMatchingTotal(counts, ['failed'])).toBe(0);
	});

	it('is null before the tallies have loaded', () => {
		expect(selectableMatchingTotal({}, [])).toBeNull();
	});
});
