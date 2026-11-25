import { expect, test } from '../fixtures/helpers';
import type { Page } from '@playwright/test';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` → AI Agents tab — the decision log's load + error states.
 *
 * `loadDecisions` only toasted on failure. On mount that left the table on
 * "No agent decisions yet" — an empty-log claim nobody had checked — and on an
 * action-chip change it left the PREVIOUS chip's rows under the newly pressed
 * chip, with no Retry anywhere. A failed first page is now stated in place
 * (`LoadError`, the queue table's pattern) and its rows are dropped.
 *
 * Everything is stubbed so the failure is deterministic.
 */

const STATS = {
	total_decisions: 1,
	auto_resolved: 0,
	escalated: 0,
	no_action: 1,
	resolution_rate: 0,
	escalation_rate: 0,
	accuracy: null
};

function decision(id: string, action: string, agentType: string) {
	return {
		id,
		exception_id: `${id}-exc`,
		invoice_id: `${id}-inv`,
		exception_type: 'duplicate',
		agent_type: agentType,
		action_taken: action,
		confidence: 0.9,
		autonomy_level: 'suggest',
		rationale: 'stubbed',
		changes: null,
		created_at: '2026-01-01T00:00:00Z'
	};
}

const json = (body: unknown, status = 200) => ({
	status,
	contentType: 'application/json',
	body: JSON.stringify(body)
});

async function stubSurroundings(page: Page) {
	await page.route('**/api/exceptions/summary*', (route) =>
		route.fulfill(json(exceptionSummary()))
	);
	await page.route('**/api/exceptions/agent-stats*', (route) => route.fulfill(json(STATS)));
	// The queue list AND the agent panel's runnable candidates (same endpoint).
	await page.route(
		(url) => url.pathname === '/api/exceptions',
		(route) => route.fulfill(json({ items: [], total: 0, page: 1, page_size: 20 }))
	);
}

test.describe('/exceptions AI Agents — decision-log load states', () => {
	test('a FAILED log load says so in place — not "no decisions" — and Retry recovers', async ({
		page
	}) => {
		await stubSurroundings(page);
		let fail = true;
		await page.route('**/api/exceptions/agent-decisions*', (route) =>
			fail
				? route.fulfill(json({ detail: 'boom' }, 500))
				: route.fulfill(
						json({
							items: [decision('d-1', 'no_action', 'RECOVERED-AGENT')],
							total: 1,
							page: 1,
							page_size: 20
						})
					)
		);

		await page.goto('/exceptions?view=agents');
		const log = page.getByTestId('agent-decision-log');
		const failed = log.getByTestId('agent-log-load-error');
		await expect(failed).toBeVisible();
		await expect(failed).toContainText('Failed to load decision log');
		// The empty-log claim is not made over a read that failed.
		await expect(log.getByTestId('table-empty')).toHaveCount(0);

		fail = false;
		await failed.getByRole('button', { name: 'Retry' }).click();
		await expect(log.getByText('RECOVERED-AGENT')).toBeVisible();
		await expect(failed).toHaveCount(0);

		// The date cell's tooltip is the formatted instant, not the wire's ISO.
		const dateCell = log.getByRole('row').filter({ hasText: 'RECOVERED-AGENT' }).locator('td').first();
		await expect(dateCell).toHaveAttribute('title', /\S/);
		await expect(dateCell).not.toHaveAttribute('title', /\d{4}-\d{2}-\d{2}T/);
	});

	test("a FAILED chip change drops the previous chip's rows", async ({ page }) => {
		await stubSurroundings(page);
		await page.route('**/api/exceptions/agent-decisions*', (route) => {
			const action = new URL(route.request().url()).searchParams.get('action_taken');
			return action === 'escalated'
				? route.fulfill(json({ detail: 'boom' }, 500))
				: route.fulfill(
						json({
							items: [decision('d-mount', 'no_action', 'MOUNT-AGENT')],
							total: 1,
							page: 1,
							page_size: 20
						})
					);
		});

		await page.goto('/exceptions?view=agents');
		const log = page.getByTestId('agent-decision-log');
		await expect(log.getByText('MOUNT-AGENT')).toBeVisible();

		await log.locator('.filter-chip', { hasText: 'Escalated' }).click();
		await expect(log.getByTestId('agent-log-load-error')).toBeVisible();
		// A no-action decision must not sit under the pressed Escalated chip.
		await expect(log.getByText('MOUNT-AGENT')).toHaveCount(0);
	});
});
