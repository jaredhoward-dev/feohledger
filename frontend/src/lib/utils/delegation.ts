/**
 * Out-of-office delegation (`/api/auth/delegation`) — the date arithmetic and
 * state reading the profile panel needs, kept free of Svelte and `$app/*` so
 * vitest's node config can reach it.
 *
 * The server stores one instant, `until`, after which the delegation stops
 * routing (`approval_chain.resolve_assignee`: `delegate_until > now`). It has no
 * start: a delegation is live from the moment it is saved. So the panel asks
 * for the LAST DAY AWAY and sends the start of the following day in the
 * reader's own time zone — a person away "through Friday" is covered for all of
 * Friday wherever they are (decisions §511).
 */

/** `GET /api/auth/delegation`. */
export interface Delegation {
	delegate_to_id: string | null;
	delegate_to_name: string | null;
	until: string | null;
	is_active: boolean;
}

/** One row of `GET /api/auth/delegation/candidates`. */
export interface DelegateCandidate {
	id: string;
	full_name: string;
}

/** What the panel says about the caller's delegation right now. */
export type DelegationState = 'none' | 'active' | 'ended';

const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** `YYYY-MM-DD` for `d` in the reader's local calendar — the value shape a
 *  `<input type="date">` reads and writes. */
export function localDay(d: Date): string {
	const y = String(d.getFullYear()).padStart(4, '0');
	const m = String(d.getMonth() + 1).padStart(2, '0');
	const day = String(d.getDate()).padStart(2, '0');
	return `${y}-${m}-${day}`;
}

/**
 * The `until` instant for a last day away: midnight at the start of the NEXT
 * local day, as a UTC ISO string (offset explicit, so the server never has to
 * guess the zone). `null` for anything that is not a real calendar day — the
 * caller shows its own message rather than send a value the server would 422.
 */
export function untilAfterLastDay(lastDay: string): string | null {
	const match = ISO_DAY.exec(lastDay);
	if (!match) return null;
	const [y, m, d] = [Number(match[1]), Number(match[2]), Number(match[3])];
	const day = new Date(y, m - 1, d);
	// Reject rollovers such as 2026-02-30 → 2026-03-02.
	if (day.getFullYear() !== y || day.getMonth() !== m - 1 || day.getDate() !== d) return null;
	return new Date(y, m - 1, d + 1).toISOString();
}

/**
 * The last local day an `until` instant still covers — the inverse of
 * {@link untilAfterLastDay}, so a delegation saved from the panel reads back as
 * the day that was chosen. One set some other way (the API, mid-day) reads as
 * the day it ends on, which it does cover in part. `null` for no or an
 * unparseable instant.
 */
export function lastDayCovered(until: string | null): string | null {
	if (!until) return null;
	const end = new Date(until);
	if (Number.isNaN(end.getTime())) return null;
	return localDay(new Date(end.getTime() - 1));
}

/**
 * `none` when no delegate is recorded; `active` when the server says it is
 * routing now; `ended` when a delegate is still recorded but its window has
 * closed (the server keeps the row, and `is_active` is false).
 */
export function delegationState(d: Delegation | null): DelegationState {
	if (!d || !d.delegate_to_id) return 'none';
	return d.is_active ? 'active' : 'ended';
}
