/**
 * The expense-specific half of `ui/ExpensePicker.svelte` and of the expenses
 * list's "Add to report" bulk action. Everything generic about the picker —
 * paging, the count line, keyboard navigation — lives in `./searchPicker.ts`.
 *
 * Background: a draft report's attach control was a free-text "Expense ID to
 * attach" box, and no screen shows an expense's id (`docs/decisions.md` §515).
 */
import type { Expense, ExpenseReport } from '#lib/types/expense.ts';
import { isAllowed } from '#lib/api/actionVerdicts.ts';

/** What the picker needs of an expense row. */
export type ExpensePickerOption = Pick<
	Expense,
	'id' | 'merchant' | 'description' | 'category' | 'expense_date' | 'amount' | 'currency'
>;

function present(text: string | null | undefined): string | null {
	const trimmed = text?.trim();
	return trimmed ? trimmed : null;
}

/**
 * The words that name an expense in the picker: its merchant, else its
 * description, else its category — the three free-text columns the server's
 * `search` matches, so what is shown is what can be typed to find it. A line
 * with none of them gets the caller's localized `untitled`, never its id.
 */
export function expenseOptionName(option: ExpensePickerOption, untitled: string): string {
	return (
		present(option.merchant) ??
		present(option.description) ??
		present(option.category) ??
		untitled
	);
}

/**
 * The committed input text: name, date and amount, so two lines from the same
 * merchant stay distinguishable once one is chosen. The formatters are the
 * caller's (locale-aware `formatDate` / `formatMoney`).
 */
export function expenseOptionLabel(
	option: ExpensePickerOption,
	untitled: string,
	format: { date: (iso: string) => string; money: (amount: Expense['amount'], currency: string) => string }
): string {
	return [
		expenseOptionName(option, untitled),
		format.date(option.expense_date),
		format.money(option.amount, option.currency)
	].join(' · ');
}

/**
 * The reports the bulk "Add to report" may target: the server's own `compose`
 * verdict (draft, and the caller owns it), not a restatement of it. The list
 * is fetched as `mine=true&status=draft`, so this drops nothing in the normal
 * case; it keeps a report the server would refuse out of the menu if the two
 * ever disagree.
 */
export function composableReports<R extends Pick<ExpenseReport, 'actions'>>(reports: R[]): R[] {
	return reports.filter((r) => isAllowed(r.actions?.compose));
}
