<script lang="ts">
	/**
	 * The report attach picker — a searchable, SERVER-PAGED combobox over the
	 * caller's own loose draft expenses (`GET /api/expenses?attachable=true`).
	 *
	 * Replaces a free-text "Expense ID to attach" box: no screen shows an
	 * expense's id, so the only way to attach was to copy one out of the API
	 * (`docs/decisions.md` §515). The set is the attach gate's own — the
	 * server narrows it to lines an attach by the caller accepts — so the
	 * picker does not offer a colleague's line to be refused.
	 *
	 * The control — reach, the honest count line, the WAI-ARIA combobox
	 * contract, the Escape capture — is `ui/SearchPicker`, as for
	 * `ui/VendorPicker` and `ui/InvoicePicker`. It always PRELOADS, so the
	 * closed field already says when there is nothing left to attach.
	 *
	 * `load` defaults to the attachable source; the parent passes a fresh
	 * function after an attach or detach changes the set, and the picker drops
	 * what it held.
	 */
	import SearchPicker from './SearchPicker.svelte';
	import Money from './Money.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { searchAttachableExpenses } from '#lib/api/expenses.ts';
	import type { SearchPickerLoad } from '#lib/utils/searchPicker.ts';
	import {
		expenseOptionLabel,
		expenseOptionName,
		type ExpensePickerOption
	} from '#lib/utils/expensePicker.ts';
	import { formatCount } from '#lib/utils/count.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { formatMoney } from '#lib/utils/money.ts';

	let {
		value = $bindable(''),
		load = searchAttachableExpenses,
		label,
		ariaLabel,
		hint,
		disabled = false,
		compact = false,
		testid,
		onselect
	}: {
		/** The chosen expense's uuid — `''` for none. */
		value?: string;
		load?: SearchPickerLoad<ExpensePickerOption>;
		label?: string;
		ariaLabel?: string;
		hint?: string;
		disabled?: boolean;
		compact?: boolean;
		testid?: string;
		onselect?: (option: ExpensePickerOption | null) => void;
	} = $props();

	const untitled = $derived(m('expenses.picker.untitled'));

	function optionLabel(option: ExpensePickerOption): string {
		return expenseOptionLabel(option, untitled, {
			date: (iso) => formatDate(iso),
			money: (amount, currency) => formatMoney(amount, { currency })
		});
	}
</script>

<SearchPicker
	bind:value
	class="expense-picker"
	preload
	{load}
	{optionLabel}
	text={{
		loading: m('expenses.picker.loading'),
		loadFailed: m('expenses.picker.loadFailed'),
		empty: m('expenses.picker.empty'),
		noMatches: (query) => m('expenses.picker.noMatches', { query }),
		showingAll: (total) => m('expenses.picker.showingAll', { total }),
		showingPartial: (shown, total) =>
			m('expenses.picker.showingPartial', { shown: formatCount(shown), total: formatCount(total) }),
		refineHint: m('expenses.picker.refineHint'),
		loadMore: m('expenses.picker.loadMore'),
		loadMoreFailed: m('expenses.picker.loadMoreFailed'),
		clearAria: m('expenses.picker.clearAria'),
		listAria: m('expenses.picker.listAria'),
		unresolvedSelection: m('expenses.picker.unresolvedSelection')
	}}
	{label}
	{ariaLabel}
	{hint}
	{disabled}
	{compact}
	{testid}
	{onselect}
>
	{#snippet option(expense)}
		<span class="ep-name">
			<span class="ep-merchant">{expenseOptionName(expense, untitled)}</span>
			<span class="ep-date">{formatDate(expense.expense_date)}</span>
		</span>
		<span class="ep-amount"><Money amount={expense.amount} currency={expense.currency} /></span>
	{/snippet}
</SearchPicker>

<style>
	.ep-name {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	.ep-merchant {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* `--text-muted` on the option's `--surface` clears 4.5:1 (the vendor
	   code's colour in the same popup); no `opacity`. */
	.ep-date {
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	.ep-amount {
		flex-shrink: 0;
		font-variant-numeric: tabular-nums;
	}
</style>
