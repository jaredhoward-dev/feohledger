<script lang="ts">
	/**
	 * A list row's due date: the EFFECTIVE date (the invoice's current payment
	 * schedule's, else its own — the date `is_overdue` and the overdue count
	 * read, `services/open_ap_due`), the overdue badge when the server flags it,
	 * and, when a schedule moved the date, the invoice's own beside it
	 * ("Due 12 Dec · rescheduled from 1 Dec"). The one renderer for the
	 * dashboard's upcoming list and the payment queue, so a row cannot print one
	 * date and flag another (decisions §565).
	 *
	 * `overdue` is the server's flag, never recomputed here: the browser's local
	 * date is not the UTC date the count is judged on.
	 */
	import Badge from './Badge.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatDate, rescheduledFrom } from '#lib/utils/time.ts';

	interface Props {
		/** The effective due date (`due_date`), `YYYY-MM-DD` or null. */
		due: string | null | undefined;
		/** The invoice's own due date (`own_due_date`). */
		own?: string | null;
		/** The server's `is_overdue` for this row. */
		overdue: boolean;
		/** The badge's text, from the caller's own namespace. */
		overdueLabel: string;
		/** `formatDate` options (e.g. no year in a compact list). */
		opts?: Intl.DateTimeFormatOptions;
	}

	let { due, own = null, overdue, overdueLabel, opts = undefined }: Props = $props();

	let from = $derived(rescheduledFrom(due, own));
	let fmt = (d: string | null | undefined) => (opts ? formatDate(d, '—', opts) : formatDate(d));
</script>

<span class="due-date" data-testid="due-date" data-rescheduled={from ? 'true' : undefined}>
	<span class="due-main">
		{fmt(due)}
		{#if overdue}
			<Badge tone="danger" variant="overdue-badge">{overdueLabel}</Badge>
		{/if}
	</span>
	{#if from}
		<span class="due-from" data-testid="due-rescheduled-from">
			{m('common.dueDate.rescheduledFrom', { date: fmt(from) })}
		</span>
	{/if}
</span>

<style>
	.due-date {
		display: inline-flex;
		flex-direction: column;
		gap: 2px;
	}

	.due-main {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}

	.due-from {
		font-size: 0.75rem;
		color: var(--text-muted);
	}
</style>
