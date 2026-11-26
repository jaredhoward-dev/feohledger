/**
 * Whose the invoice is, for the review footer's note — never whether the reader
 * may act on it.
 *
 * Assignment is routing, not authority (decisions §564): `POST
 * /api/invoices/{id}/approve|reject` accept any approver who passes the
 * server's gates (permission, approval limits, named approvers and chain
 * levels, segregation of duties, delegation), and the modal offers Approve /
 * Reject exactly as `GET /review-actions` says. A reader who is not the
 * assignee still gets the buttons, with a note naming the colleague so they do
 * not step on someone else's queue by accident.
 */

interface Assignable {
	assigned_to_id?: string | null;
	assigned_to?: string | null;
}

/**
 * The colleague's display name when the invoice is assigned to someone other
 * than `viewerId`, else `null` (unassigned, or assigned to the reader). An id
 * with no stored name still counts as "someone else" (`name: null`), and the
 * caller supplies a generic label.
 */
export function assignedElsewhere(
	invoice: Assignable,
	viewerId: string | null | undefined
): { name: string | null } | null {
	const assignee = invoice.assigned_to_id;
	if (!assignee || assignee === viewerId) return null;
	const name = invoice.assigned_to?.trim();
	return { name: name ? name : null };
}
