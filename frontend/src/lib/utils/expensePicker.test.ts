import { describe, expect, it } from 'vitest';
import {
	composableReports,
	expenseOptionLabel,
	expenseOptionName,
	isAttachableLine,
	splitAttachable,
	type AttachableRow,
	type ExpensePickerOption
} from './expensePicker';

const line = (over: Partial<ExpensePickerOption> = {}): ExpensePickerOption => ({
	id: 'e-1',
	merchant: null,
	description: null,
	category: null,
	expense_date: '2026-06-04',
	amount: '12.00',
	currency: 'USD',
	...over
});

describe('expenseOptionName', () => {
	it('names a line by merchant, then description, then category', () => {
		expect(expenseOptionName(line({ merchant: 'Cab Co', description: 'Airport', category: 'travel' }), 'Untitled')).toBe('Cab Co');
		expect(expenseOptionName(line({ description: 'Airport', category: 'travel' }), 'Untitled')).toBe('Airport');
		expect(expenseOptionName(line({ category: 'travel' }), 'Untitled')).toBe('travel');
	});

	it('skips blank text and never falls back to the id', () => {
		expect(expenseOptionName(line({ merchant: '   ', description: '' }), 'Untitled')).toBe('Untitled');
		expect(expenseOptionName(line(), 'Untitled')).not.toContain('e-1');
	});
});

describe('expenseOptionLabel', () => {
	it('joins name, the caller-formatted date and amount', () => {
		const label = expenseOptionLabel(line({ merchant: 'Cab Co' }), 'Untitled', {
			date: (iso) => `D(${iso})`,
			money: (amount, currency) => `${currency} ${amount}`
		});
		expect(label).toBe('Cab Co · D(2026-06-04) · USD 12.00');
	});
});

describe('composableReports', () => {
	const refusal = { code: 'expense_report_not_draft', message: 'x', params: {} };
	it('keeps only reports whose compose verdict is null', () => {
		const ok = { id: 'r1', actions: { submit: null, approve: null, reject: null, compose: null } };
		const refused = { id: 'r2', actions: { submit: null, approve: null, reject: null, compose: refusal } };
		const noVerdict = { id: 'r3', actions: undefined };
		expect(composableReports([ok, refused, noVerdict]).map((r) => r.id)).toEqual(['r1']);
	});
});

const ME = 'u-me';
const row = (id: string, over: Partial<AttachableRow> = {}): AttachableRow => ({
	id,
	report_id: null,
	status: 'draft',
	created_by_user_id: ME,
	...over
});

describe('isAttachableLine', () => {
	it('takes the caller\'s own loose draft line and an unrecorded one', () => {
		expect(isAttachableLine(row('a'), ME)).toBe(true);
		expect(isAttachableLine(row('a', { created_by_user_id: null }), ME)).toBe(true);
	});

	it('refuses a line on a report, a non-draft line and a colleague\'s line', () => {
		expect(isAttachableLine(row('a', { report_id: 'r-1' }), ME)).toBe(false);
		expect(isAttachableLine(row('a', { status: 'submitted' }), ME)).toBe(false);
		expect(isAttachableLine(row('a', { created_by_user_id: 'u-other' }), ME)).toBe(false);
		expect(isAttachableLine(row('a', { created_by_user_id: 'u-other' }), null)).toBe(false);
	});
});

describe('splitAttachable', () => {
	const rows = [
		row('mine'),
		row('on-report', { report_id: 'r-2' }),
		row('theirs', { created_by_user_id: 'u-other' }),
		row('submitted', { status: 'submitted' })
	];

	it('sends only attachable loaded rows and counts the rest as skipped', () => {
		expect(splitAttachable(['mine', 'on-report', 'theirs', 'submitted'], rows, ME)).toEqual({
			attachable: ['mine'],
			skipped: 3
		});
	});

	it('skips an id with no loaded row rather than guessing', () => {
		expect(splitAttachable(['mine', 'unloaded'], rows, ME)).toEqual({
			attachable: ['mine'],
			skipped: 1
		});
	});

	it('lets the server\'s attachable set decide an all-matching selection', () => {
		const server = new Set(['unloaded', 'mine']);
		expect(splitAttachable(['mine', 'unloaded', 'on-report'], rows, ME, server)).toEqual({
			attachable: ['mine', 'unloaded'],
			skipped: 1
		});
	});

	it('reports nothing attachable as an empty send', () => {
		expect(splitAttachable(['on-report'], rows, ME)).toEqual({ attachable: [], skipped: 1 });
		expect(splitAttachable([], rows, ME)).toEqual({ attachable: [], skipped: 0 });
	});
});
