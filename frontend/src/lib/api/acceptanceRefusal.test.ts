import { describe, expect, it, vi } from 'vitest';
import {
	acceptanceRefusal,
	employeeAcceptanceRefusal,
	portalAcceptanceRefusal,
	LEGAL_ACCEPTANCE_REQUIRED,
	PRIVACY_ACKNOWLEDGEMENT_REQUIRED
} from './acceptanceRefusal.ts';

describe('acceptanceRefusal', () => {
	it('runs the handler on its own 403 code only', () => {
		const route = acceptanceRefusal(LEGAL_ACCEPTANCE_REQUIRED);
		const handler = vi.fn();
		route.listen(handler);

		expect(route.notice(403, LEGAL_ACCEPTANCE_REQUIRED)).toBe(true);
		expect(handler).toHaveBeenCalledTimes(1);

		// The accept endpoint's 422 with the same code is the step's own
		// refusal to state, not a reason to (re)draw the step.
		expect(route.notice(422, LEGAL_ACCEPTANCE_REQUIRED)).toBe(false);
		expect(route.notice(403, 'token_tenant_mismatch')).toBe(false);
		expect(route.notice(403, null)).toBe(false);
		expect(route.notice(401, null)).toBe(false);
		expect(handler).toHaveBeenCalledTimes(1);
	});

	it('is a no-op before a store listens', () => {
		expect(acceptanceRefusal(LEGAL_ACCEPTANCE_REQUIRED).notice(403, LEGAL_ACCEPTANCE_REQUIRED)).toBe(
			true
		);
	});

	it('keeps the employee and portal gates apart', () => {
		const employee = vi.fn();
		const portal = vi.fn();
		employeeAcceptanceRefusal.listen(employee);
		portalAcceptanceRefusal.listen(portal);

		expect(employeeAcceptanceRefusal.notice(403, PRIVACY_ACKNOWLEDGEMENT_REQUIRED)).toBe(false);
		expect(portalAcceptanceRefusal.notice(403, LEGAL_ACCEPTANCE_REQUIRED)).toBe(false);
		portalAcceptanceRefusal.notice(403, PRIVACY_ACKNOWLEDGEMENT_REQUIRED);

		expect(employee).not.toHaveBeenCalled();
		expect(portal).toHaveBeenCalledTimes(1);
	});
});
