import { describe, expect, it } from 'vitest';
import { delegationState, lastDayCovered, localDay, untilAfterLastDay } from './delegation.ts';

describe('untilAfterLastDay', () => {
	it('ends at the start of the next local day', () => {
		const until = untilAfterLastDay('2026-12-04');
		expect(until).toBe(new Date(2026, 11, 5).toISOString());
	});

	it('crosses a month and a year end', () => {
		expect(untilAfterLastDay('2026-12-31')).toBe(new Date(2027, 0, 1).toISOString());
	});

	it.each(['', '2026-13-01', '2026-02-30', '04/12/2026', 'tomorrow'])(
		'refuses %j rather than send a value the server would 422',
		(raw) => {
			expect(untilAfterLastDay(raw)).toBeNull();
		}
	);
});

describe('lastDayCovered', () => {
	it('reads back the day the panel saved', () => {
		expect(lastDayCovered(untilAfterLastDay('2026-12-04'))).toBe('2026-12-04');
	});

	it('reads a mid-day end as the day it ends on', () => {
		expect(lastDayCovered(new Date(2026, 11, 4, 15, 30).toISOString())).toBe('2026-12-04');
	});

	it('is null for nothing or garbage', () => {
		expect(lastDayCovered(null)).toBeNull();
		expect(lastDayCovered('not a date')).toBeNull();
	});
});

describe('localDay', () => {
	it('is the date-input value shape in the local calendar', () => {
		expect(localDay(new Date(2026, 0, 9, 23, 59))).toBe('2026-01-09');
	});
});

describe('delegationState', () => {
	const base = { delegate_to_id: 'u-1', delegate_to_name: 'Ana', until: '2026-12-05T00:00:00Z' };

	it('is none with no delegate recorded', () => {
		expect(delegationState(null)).toBe('none');
		expect(
			delegationState({ delegate_to_id: null, delegate_to_name: null, until: null, is_active: false })
		).toBe('none');
	});

	it('follows the server on whether it is routing now', () => {
		expect(delegationState({ ...base, is_active: true })).toBe('active');
		expect(delegationState({ ...base, is_active: false })).toBe('ended');
	});
});
