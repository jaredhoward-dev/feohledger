<script lang="ts">
	/**
	 * An expense line's Status cell: its status badge, the pre-approval it draws
	 * (decisions §563) and its policy-violation count. One owner for the
	 * Expenses table and the report-detail line table, which had grown the same
	 * markup twice (guard rail 9).
	 *
	 * The pre-approved chip states the drawn amount as text, in the line's own
	 * currency: a figure only in a `title` tooltip is unreachable by keyboard,
	 * touch and most screen readers. It wears the `muted` tone, not `success`,
	 * so the line's own status stays the one green signal in the cell. The
	 * violation chip keeps its `title` (the list of violations is long) and the
	 * help cursor that advertises it; the pre-approved chip has nothing to
	 * hover for and gets neither.
	 */
	import Badge from './Badge.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { policyViolationText } from '#lib/api/policyViolations.ts';
	import {
		drawsPreapproval,
		expenseStatusLabelKey,
		EXPENSE_STATUS_TONES,
		type Expense,
		type ExpenseStatus
	} from '#lib/types/expense.ts';
	import { formatMoney } from '#lib/utils/money.ts';

	interface Props {
		expense: Pick<
			Expense,
			'status' | 'currency' | 'policy_violations' | 'preapproval_id' | 'preapproval_drawn_amount'
		>;
	}

	let { expense }: Props = $props();

	// An unrecognised status from the API renders raw rather than blank.
	let statusText = $derived.by(() => {
		const key = expenseStatusLabelKey(expense.status);
		return key ? m(key) : expense.status;
	});
	let violations = $derived(expense.policy_violations ?? []);
</script>

<span class="status-cell">
	<Badge tone={EXPENSE_STATUS_TONES[expense.status as ExpenseStatus]} variant={expense.status}
		>{statusText}</Badge
	>
	{#if drawsPreapproval(expense)}
		<span class="preapproval-chip" data-testid="expense-preapproval-draw">
			<Badge tone="muted" variant="preapproval-draw"
				>{m('expenses.preapprovalDraw.badge', {
					amount: formatMoney(expense.preapproval_drawn_amount, { currency: expense.currency })
				})}</Badge
			>
		</span>
	{/if}
	{#if violations.length}
		<span class="violation-chip">
			<Badge
				tone="danger"
				variant="violation"
				title={violations.map((v) => policyViolationText(v, m)).join('; ')}
				>⚠ {violations.length}</Badge
			>
		</span>
	{/if}
</span>

<style>
	/* The 6px gap is the cell's placement, not the pills': `<Badge>` owns
	   colour and metrics, the caller owns where they sit. Wraps so a narrow
	   column stacks the chips rather than overflowing (WCAG 1.4.10). */
	.status-cell {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

	.preapproval-chip {
		display: inline-flex;
	}

	/* `cursor` is inherited, so the caller can own the affordance without
	   `Badge` growing a prop for it. The pill's whole job is its `title`
	   tooltip, which is what the help cursor advertises. */
	.violation-chip {
		display: inline-flex;
		cursor: help;
	}
</style>
