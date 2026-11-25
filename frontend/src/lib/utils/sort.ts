/**
 * Column-sort state shared by the five primary list pages (invoices,
 * vendors, payments, expenses, contracts). Each page owns its own
 * `sortField` / `sortOrder` `$state` and URL sync (mirroring the existing
 * per-page `syncUrl()` pattern for filters) — this module is just the pure
 * click-to-toggle rule so the five copies can't drift.
 */

export type SortOrder = 'asc' | 'desc';

export interface SortState {
	field: string | null;
	order: SortOrder;
}

/**
 * Clicking a column header that isn't the active sort starts it ascending;
 * clicking the ALREADY-active column flips its direction. Mirrors the
 * common spreadsheet/table convention and keeps every list page consistent.
 */
export function toggleSort(current: SortState, field: string): SortState {
	if (current.field !== field) return { field, order: 'asc' };
	return { field, order: current.order === 'asc' ? 'desc' : 'asc' };
}

/**
 * Clearing a column sort returns the list to the backend's own default order
 * (`field: null`) — the state a page starts in before any header is clicked.
 * The direction is kept but unused while no field is set, so the next header
 * click still starts ascending (`toggleSort`).
 */
export function clearSort(current: SortState): SortState {
	return { field: null, order: current.order };
}

/**
 * Read a page's sort back out of its URL (`?sort=&order=`), keeping only a
 * field the page actually offers as a sortable header. The URL is user-
 * editable and outlives renames: an unknown `sort=` passed straight through
 * is a 422 from the backend's allowlist, and a list page that fails its load
 * on every Retry is a dead end with no control on screen to undo it. An
 * unknown field falls back to the default order; any `order` other than
 * `desc` reads as ascending.
 */
export function sortFromUrl(
	params: Pick<URLSearchParams, 'get'>,
	fields: readonly string[]
): SortState {
	const field = params.get('sort');
	return {
		field: field && fields.includes(field) ? field : null,
		order: params.get('order') === 'desc' ? 'desc' : 'asc'
	};
}
