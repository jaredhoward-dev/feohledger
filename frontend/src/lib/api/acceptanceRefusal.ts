/**
 * The server-side Terms / Privacy gate's refusal, routed to the accept step.
 *
 * While a signed-in user owes an acceptance, every gated call answers 403 coded
 * `legal_acceptance_required` (employees) or `privacy_acknowledgement_required`
 * (supplier portal) — `docs/decisions.md` §505. The page's answer to either is
 * its accept step, not an error toast: the HTTP client `notice`s every refused
 * response, and the auth store `listen`s and sets the account's
 * `legal_acceptance_required`, so the layout draws the step in the shell's
 * place. Only the code is read, never the message. Pure (imports nothing from
 * `$app/*`), so the node vitest config reaches it.
 */

/** `backend/app/services/legal_acceptance.py::LEGAL_ACCEPTANCE_REQUIRED`. */
export const LEGAL_ACCEPTANCE_REQUIRED = 'legal_acceptance_required';
/** `backend/app/services/portal_legal_acceptance.py::PRIVACY_ACKNOWLEDGEMENT_REQUIRED`. */
export const PRIVACY_ACKNOWLEDGEMENT_REQUIRED = 'privacy_acknowledgement_required';

export interface AcceptanceRefusal {
	/** The store's handler; a later call replaces an earlier one. */
	listen(handler: () => void): void;
	/** Called by the HTTP client on a refused response: runs the handler and
	 *  returns true when it is this gate's 403. */
	notice(status: number, code: string | null): boolean;
}

export function acceptanceRefusal(code: string): AcceptanceRefusal {
	let handler: (() => void) | null = null;
	return {
		listen(fn) {
			handler = fn;
		},
		notice(status, refusalCode) {
			if (status !== 403 || refusalCode !== code) return false;
			handler?.();
			return true;
		}
	};
}

/** The employee app's gate (`api/deps.get_current_user`). */
export const employeeAcceptanceRefusal = acceptanceRefusal(LEGAL_ACCEPTANCE_REQUIRED);
/** The supplier portal's gate (`api/portal_deps.get_current_vendor_user`). */
export const portalAcceptanceRefusal = acceptanceRefusal(PRIVACY_ACKNOWLEDGEMENT_REQUIRED);
