import { describe, it, expect } from 'vitest';
import { clearSort, sortFromUrl, toggleSort } from './sort';

describe('toggleSort', () => {
	it('starts a newly-clicked column ascending', () => {
		const next = toggleSort({ field: null, order: 'desc' }, 'amount');
		expect(next).toEqual({ field: 'amount', order: 'asc' });
	});

	it('switching to a different column always starts ascending, regardless of the old order', () => {
		const next = toggleSort({ field: 'amount', order: 'desc' }, 'vendor_name');
		expect(next).toEqual({ field: 'vendor_name', order: 'asc' });
	});

	it('clicking the already-active ascending column flips it to descending', () => {
		const next = toggleSort({ field: 'amount', order: 'asc' }, 'amount');
		expect(next).toEqual({ field: 'amount', order: 'desc' });
	});

	it('clicking the already-active descending column flips it to ascending', () => {
		const next = toggleSort({ field: 'amount', order: 'desc' }, 'amount');
		expect(next).toEqual({ field: 'amount', order: 'asc' });
	});
});

describe('clearSort', () => {
	it('drops the field, returning the list to its default order', () => {
		expect(clearSort({ field: 'vendor_name', order: 'desc' })).toEqual({
			field: null,
			order: 'desc',
		});
	});

	it('leaves the next header click starting ascending', () => {
		const cleared = clearSort({ field: 'amount', order: 'desc' });
		expect(toggleSort(cleared, 'amount')).toEqual({ field: 'amount', order: 'asc' });
	});
});

describe('sortFromUrl', () => {
	const FIELDS = ['name', 'code', 'status'];
	const read = (qs: string) => sortFromUrl(new URLSearchParams(qs), FIELDS);

	it('restores an offered field and its direction', () => {
		expect(read('sort=code&order=desc')).toEqual({ field: 'code', order: 'desc' });
	});

	it('defaults to ascending when the order is missing', () => {
		expect(read('sort=status')).toEqual({ field: 'status', order: 'asc' });
	});

	it('drops a field the page does not offer (the backend would 422 it)', () => {
		expect(read('sort=bank_account&order=desc')).toEqual({ field: null, order: 'desc' });
	});

	it('reads an unknown order as ascending rather than passing it through', () => {
		expect(read('sort=name&order=sideways')).toEqual({ field: 'name', order: 'asc' });
	});

	it('is the default order when the URL carries no sort', () => {
		expect(read('')).toEqual({ field: null, order: 'asc' });
	});

	it('falls back to the page default direction when one is given', () => {
		const params = new URLSearchParams('sort=code&order=sideways');
		expect(sortFromUrl(params, FIELDS, 'desc')).toEqual({ field: 'code', order: 'desc' });
		expect(sortFromUrl(new URLSearchParams('sort=code&order=asc'), FIELDS, 'desc')).toEqual({
			field: 'code',
			order: 'asc'
		});
	});
});
