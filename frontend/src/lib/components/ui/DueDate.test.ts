import { render } from 'svelte/server';
import { describe, expect, it, vi } from 'vitest';

// The component reaches the i18n store, which reads `$app/env` — a SvelteKit
// virtual module the plain vitest config does not provide.
vi.mock('$app/env', () => ({ browser: false }));

import DueDate from './DueDate.svelte';

/**
 * The one due-date cell for the dashboard's upcoming list and the payment
 * queue (decisions §565): the effective date, the server's overdue flag, and
 * the invoice's own date only when a schedule moved it.
 */
function html(props: Record<string, unknown>): string {
	return render(DueDate, {
		props: { due: '2026-12-12', overdue: false, overdueLabel: 'Overdue', ...props }
	}).body;
}

describe('DueDate', () => {
	it('names the own date when a schedule moved it', () => {
		const out = html({ own: '2026-12-01' });
		expect(out).toContain('data-testid="due-rescheduled-from"');
		expect(out).toContain('rescheduled from');
		expect(out).toContain('data-rescheduled="true"');
	});

	it('shows one date when the two match or the own date is unknown', () => {
		expect(html({ own: '2026-12-12' })).not.toContain('due-rescheduled-from');
		expect(html({ own: null })).not.toContain('due-rescheduled-from');
	});

	it('renders the overdue badge from the server flag alone', () => {
		expect(html({ overdue: true })).toContain('Overdue');
		expect(html({ overdue: false })).not.toContain('Overdue');
	});
});
