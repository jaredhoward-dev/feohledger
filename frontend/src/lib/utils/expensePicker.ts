/**
 * The expense-specific half of `ui/ExpensePicker.svelte` and of the expenses
 * list's "Add to report" bulk action. Everything generic about the picker —
 * paging, the count line, keyboard navigation — lives in `./searchPicker.ts`.
 *
 * Background: a draft report's attach control was a free-text "Expense ID to
 * attach" box, and no screen shows an expense's id (`docs/decisions.md` §515).
 */
import { isLineCreator, type Expense, type ExpenseReport } from '#lib/types/expense.ts';
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

/** What the bulk "Add to report" reads of a loaded row. */
export type AttachableRow = Pick<Expense, 'id' | 'report_id' | 'status' | 'created_by_user_id'>;

/**
 * Whether an attach by `userId` would take this line: on no report, `draft`,
 * and theirs or unrecorded — the server's `api/expenses._attachable_by`, read
 * from a loaded row. A mirror for deciding what to send; the server still
 * gates every line it is sent.
 */
export function isAttachableLine(row: AttachableRow, userId: string | null | undefined): boolean {
	return row.report_id == null && row.status === 'draft' && isLineCreator(row, userId);
}

/**
 * Split a bulk selection into the ids the "Add to report" sends and a count
 * of the rest, which it reports as skipped (decisions §515). A line already on
 * a report is skipped, never moved: the endpoint would re-home it silently.
 *
 * `matchingAttachable` is the server's own attachable set
 * (`GET /api/expenses/ids?attachable=true`) for a "select all N matching"
 * selection, which reaches rows the page never loaded; when given it decides
 * every id. Otherwise each id is judged from its loaded row, and an id with
 * no loaded row is skipped (nothing shows it is attachable).
 */
export function splitAttachable(
	selected: Iterable<string>,
	rows: readonly AttachableRow[],
	userId: string | null | undefined,
	matchingAttachable: ReadonlySet<string> | null = null
): { attachable: string[]; skipped: number } {
	const byId = new Map(rows.map((r) => [r.id, r]));
	const attachable: string[] = [];
	let skipped = 0;
	for (const id of selected) {
		const ok = matchingAttachable
			? matchingAttachable.has(id)
			: (() => {
					const row = byId.get(id);
					return row != null && isAttachableLine(row, userId);
				})();
		if (ok) attachable.push(id);
		else skipped += 1;
	}
	return { attachable, skipped };
}
