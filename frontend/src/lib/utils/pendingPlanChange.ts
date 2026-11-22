/**
 * The plan a card is being added for, kept across a Stripe REDIRECT
 * confirmation of the card.
 *
 * The plan dialog asks for a card when the chosen paid plan needs one, then
 * moves on to the price review — saving a card NEVER changes the plan or
 * charges anything; only the reader's "Confirm" does (decisions §482). A card
 * confirms in place, so that is the whole story — except for a redirect-based
 * method, where Stripe navigates away and back to
 * `/billing?setup_intent=…&redirect_status=…` and every piece of state is
 * gone. So the plan code is stashed in `sessionStorage` right before
 * `confirmSetup` and taken back on the return, where the page reopens the
 * dialog at the review step for it.
 *
 * The stash names the SetupIntent it belongs to and is honoured only for that
 * one: a stale entry (an abandoned attempt in this tab) can never reopen a
 * review on some later, unrelated return. It holds a plan code and an intent
 * id — neither is a secret. Pure apart from the injected storage, so vitest's
 * node environment reaches all of it; blocked storage only costs the resume.
 */

export const PENDING_PLAN_KEY = 'billing_pending_plan';

export type PendingPlanStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function defaultStorage(): PendingPlanStorage | null {
	try {
		return typeof sessionStorage === 'undefined' ? null : sessionStorage;
	} catch {
		return null;
	}
}

/**
 * Stash `planCode` for the SetupIntent about to be confirmed, or clear any
 * stash when there is no pending plan — always one or the other, so an
 * earlier attempt can never steer this one.
 */
export function rememberPendingPlan(
	planCode: string | null | undefined,
	setupIntentId: string | null | undefined,
	storage: PendingPlanStorage | null = defaultStorage()
): void {
	if (!storage) return;
	try {
		if (planCode && setupIntentId) {
			storage.setItem(PENDING_PLAN_KEY, JSON.stringify({ planCode, setupIntentId }));
		} else {
			storage.removeItem(PENDING_PLAN_KEY);
		}
	} catch {
		// Quota / blocked storage: the card still saves, the resume is lost.
	}
}

/** Read and clear the stash; the plan code only when it was stashed for
 *  `setupIntentId`, else `null`. Always clears. */
export function takePendingPlan(
	setupIntentId: string | null | undefined,
	storage: PendingPlanStorage | null = defaultStorage()
): string | null {
	if (!storage) return null;
	try {
		const raw = storage.getItem(PENDING_PLAN_KEY);
		storage.removeItem(PENDING_PLAN_KEY);
		if (!raw || !setupIntentId) return null;
		const parsed: unknown = JSON.parse(raw);
		if (!parsed || typeof parsed !== 'object') return null;
		const { planCode, setupIntentId: stashedFor } = parsed as Record<string, unknown>;
		if (typeof planCode !== 'string' || !planCode || stashedFor !== setupIntentId) return null;
		return planCode;
	} catch {
		return null;
	}
}
