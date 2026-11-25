import { describe, expect, it } from 'vitest';
import { assignedElsewhere } from './reviewAssignment.ts';

describe('assignedElsewhere', () => {
	it('is null for an unassigned invoice', () => {
		expect(assignedElsewhere({ assigned_to_id: null, assigned_to: null }, 'me')).toBeNull();
		expect(assignedElsewhere({}, 'me')).toBeNull();
	});

	it('is null when the reader is the assignee', () => {
		expect(assignedElsewhere({ assigned_to_id: 'me', assigned_to: 'Me Myself' }, 'me')).toBeNull();
	});

	it('names the colleague when assigned to someone else', () => {
		expect(assignedElsewhere({ assigned_to_id: 'them', assigned_to: 'Dana Lee' }, 'me')).toEqual({
			name: 'Dana Lee'
		});
	});

	it('still reports someone else when the stored name is blank', () => {
		expect(assignedElsewhere({ assigned_to_id: 'them', assigned_to: '  ' }, 'me')).toEqual({
			name: null
		});
	});

	it('treats an unknown reader as not the assignee', () => {
		expect(assignedElsewhere({ assigned_to_id: 'them', assigned_to: 'Dana' }, undefined)).toEqual({
			name: 'Dana'
		});
	});
});
