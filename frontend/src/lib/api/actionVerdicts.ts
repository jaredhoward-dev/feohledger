/**
 * Action verdicts — the server's answer to "would this action be accepted?",
 * read BEFORE the page offers the control (issue #575 § One rule, one place).
 *
 * A page used to decide whether to show Approve, Execute, Void, … by restating
 * the endpoint's rule (a status list, a role list, `uploaded_by !== me`), and
 * each copy drifted from the server in its own way: the uploader shown an
 * Approve that 403s, the run's creator shown an Execute that 403s, an
 * unverified vendor's invoice shown no way to be recorded paid at all (#573).
 * The backend now computes each verdict by running the SAME gate functions the
 * endpoint enforces with, and ships it as a coded refusal (`{code, message,
 * params}` — `backend/app/api/refusals.py::coded_refusal`) or `null`.
 *
 * Rules for a page reading one:
 *
 *  - **`null` means offer it; a refusal means do not** — render the control
 *    disabled with {@link refusalText} beside it when the reader can act on the
 *    reason (a colleague must approve), or not at all when it is about the
 *    reader's role (`role_not_permitted`).
 *  - **A verdict that has not arrived is a refusal** (fail closed): offer
 *    nothing until the server has answered.
 *  - **Never restate the rule beside it.** A second check in the page is
 *    exactly the copy this replaces.
 */
import { localizeApiDetail } from '#lib/api/codedRefusals.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

/** A coded refusal the endpoint would answer with, or `null` when it accepts. */
export type ActionRefusal = {
	code: string;
	message: string;
	params?: Record<string, unknown>;
} | null;

/** The code for "your role / permissions do not allow this" — a reason the
 *  reader cannot act on, so the control is hidden rather than explained. */
export const ROLE_NOT_PERMITTED = 'role_not_permitted';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/** The sentence to show for a refusal: localized from its code when this build
 *  knows it, else the server's English. Empty for `null`. */
export function refusalText(refusal: ActionRefusal, t: Translate): string {
	if (!refusal) return '';
	return localizeApiDetail(refusal, t) ?? refusal.message;
}

/** Whether a control should be rendered at all: hidden only when the verdict
 *  is unknown or refuses the reader's role. */
export function isOffered(refusal: ActionRefusal | undefined): boolean {
	return refusal !== undefined && refusal?.code !== ROLE_NOT_PERMITTED;
}

/** Whether a rendered control may be pressed. */
export function isAllowed(refusal: ActionRefusal | undefined): boolean {
	return refusal === null;
}

/** How a bulk action would land over a selection, from the rows' verdicts. */
export interface BulkVerdictTally {
	/** Selected rows whose verdict accepts the action. */
	allowed: number;
	/** Selected rows whose verdict refuses it — the server will skip them. */
	refused: number;
	/** Selected ids with no loaded row (a "select all N matching" beyond the
	 *  pages fetched): the server decides, so they neither hide the action nor
	 *  count as skips. */
	unknown: number;
}

/**
 * Tally one bulk action over the selected ids, reading each loaded row's
 * verdict (`verdictOf`) — the bulk counterpart of {@link isAllowed}, so a bulk
 * bar offers an action only when some selected row would take it
 * (`offersBulk`) and can say up front how many the server will skip
 * (decisions §508). Never restates the rule: a row is allowed exactly when its
 * own verdict is `null`.
 */
export function tallyBulkVerdicts<R extends { id: string }>(
	selectedIds: Iterable<string>,
	rows: readonly R[],
	verdictOf: (row: R) => ActionRefusal | undefined
): BulkVerdictTally {
	const byId = new Map(rows.map((r) => [r.id, r]));
	const tally: BulkVerdictTally = { allowed: 0, refused: 0, unknown: 0 };
	for (const id of selectedIds) {
		const row = byId.get(id);
		if (!row) tally.unknown += 1;
		else if (isAllowed(verdictOf(row))) tally.allowed += 1;
		else tally.refused += 1;
	}
	return tally;
}

/** Whether a bulk action is worth offering: some selected row would take it,
 *  or some selected row's verdict is not loaded (the server will decide). */
export function offersBulk(tally: BulkVerdictTally): boolean {
	return tally.allowed > 0 || tally.unknown > 0;
}
