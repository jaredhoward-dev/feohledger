import { describe, expect, it } from 'vitest';
import { EN_ALL as en } from '#lib/i18n/allMessages.ts';
import {
	AUTONOMY_LEVELS,
	LINE_TOTAL_RULE,
	autonomyHintKey,
	autonomyLabelKey,
	withoutOwnedFraudKeys
} from './orgControls.ts';

describe('withoutOwnedFraudKeys', () => {
	it('drops only the line-total rule, leaving the input untouched', () => {
		const rules = { round_amount_enabled: false, [LINE_TOTAL_RULE]: true, rush_payment_max_days: 3 };
		const out = withoutOwnedFraudKeys(rules);
		expect(out).toEqual({ round_amount_enabled: false, rush_payment_max_days: 3 });
		expect(rules[LINE_TOTAL_RULE]).toBe(true);
	});

	it('is a no-op copy when the key is absent', () => {
		expect(withoutOwnedFraudKeys({ a: 1 })).toEqual({ a: 1 });
	});
});

describe('autonomy levels', () => {
	it('lists the backend levels, safest first', () => {
		expect([...AUTONOMY_LEVELS]).toEqual(['conservative', 'balanced', 'aggressive']);
	});

	it('has a label and a hint in the catalogue for every level', () => {
		for (const level of AUTONOMY_LEVELS) {
			expect(en[autonomyLabelKey(level)]).toBeTruthy();
			expect(en[autonomyHintKey(level)]).toBeTruthy();
		}
	});
});
