import { describe, expect, it } from 'vitest';
import {
	compareDecimal,
	inspectionChoice,
	inspectionFromChoice,
	isLooserThanOrganization,
	organizationTolerance,
	toleranceBody,
	type MatchRules
} from './matchRules.ts';

function rules(orgTolerance: string | null): MatchRules {
	return {
		organization: { tolerance_pct: orgTolerance, require_inspection: null },
		built_in: { tolerance_pct: '5', require_inspection: false },
		vendor_rules: [],
		commodity_rules: []
	};
}

describe('inspection choice', () => {
	it('round-trips the three states', () => {
		for (const value of [null, true, false]) {
			expect(inspectionFromChoice(inspectionChoice(value))).toBe(value);
		}
	});
});

describe('toleranceBody', () => {
	it('sends blank as null (inherit) and trims', () => {
		expect(toleranceBody('')).toBeNull();
		expect(toleranceBody('   ')).toBeNull();
		expect(toleranceBody(' 2.5 ')).toBe('2.5');
	});

	it('reads a comma decimal separator as a point', () => {
		expect(toleranceBody('2,5')).toBe('2.5');
	});

	it('passes anything else through for the server to judge', () => {
		// The range and precision rules are the server's (coded refusals).
		expect(toleranceBody('150')).toBe('150');
		expect(toleranceBody('abc')).toBe('abc');
	});
});

describe('compareDecimal', () => {
	it.each([
		['2.5', '2.50', 0],
		['10', '9.99', 1],
		['0.5', '5', -1],
		['05', '5', 0],
		['1.005', '1.01', -1]
	])('%s vs %s', (a, b, expected) => {
		expect(compareDecimal(a, b)).toBe(expected);
	});
});

describe('organization tolerance and looser rules', () => {
	it('falls back to the built-in when the organization sets none', () => {
		expect(organizationTolerance(rules(null))).toBe('5');
		expect(organizationTolerance(rules('3'))).toBe('3');
	});

	it('flags only a rule whose own tolerance is wider', () => {
		const r = rules('3');
		expect(isLooserThanOrganization({ tolerance_pct: '3.5', require_inspection: null }, r)).toBe(
			true
		);
		expect(isLooserThanOrganization({ tolerance_pct: '3', require_inspection: null }, r)).toBe(
			false
		);
		expect(isLooserThanOrganization({ tolerance_pct: '1', require_inspection: null }, r)).toBe(
			false
		);
		// A rule that sets no tolerance inherits — never looser.
		expect(isLooserThanOrganization({ tolerance_pct: null, require_inspection: true }, r)).toBe(
			false
		);
	});
});
