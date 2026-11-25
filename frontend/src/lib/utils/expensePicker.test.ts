import { describe, expect, it } from 'vitest';
import {
	composableReports,
	expenseOptionLabel,
	expenseOptionName,
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
