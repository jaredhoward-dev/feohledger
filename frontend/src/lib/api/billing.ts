// Typed helpers for the platform billing surface. Routes through the shared
// `api` client (Bearer + X-Tenant-Slug + 401-bounce). See
// `#lib/types/billing` for the response contracts and `backend/docs/billing.md`
// for the backend slice this consumes.
import { api } from '#lib/api.ts';
import { planChangeRequestFrom } from '#lib/types/billing.ts';
import type {
	BillingInvoicesResponse,
	BillingKeepPlanResponse,
	BillingPaymentMethodsResponse,
	BillingPlanChangePreview,
	BillingPlanChangeResponse,
	BillingPlansResponse,
	BillingSetDefaultPaymentMethodRequest,
	BillingSetupIntentResponse,
	BillingSpendCapResponse,
	BillingSubscriptionResponse
} from '#lib/types/billing.ts';

/** Set (exact decimal string) or clear (`null`) the org's monthly AI-read
 *  overage spending cap. Admin only. Returns the cap as stored plus this
 *  month's AI usage re-priced under it. */
export function setBillingSpendCap(cap: string | null): Promise<BillingSpendCapResponse> {
	return api.put<BillingSpendCapResponse>('/api/billing/spending-cap', {
		monthly_spend_cap: cap
	});
}

/** Current plan + subscription status + usage-to-date for the active period.
 *  `plan`/`subscription` are null when the org has no live subscription. */
export function getBillingSubscription(): Promise<BillingSubscriptionResponse> {
	return api.get<BillingSubscriptionResponse>('/api/billing/subscription');
}

/** The org's past platform-billing invoices / receipts (newest first). admin/cfo
 *  only (the backend 403s everyone else). An org never provisioned with the
 *  provider — or an unconfigured / unavailable one — yields an empty list. */
export function getBillingInvoices(): Promise<BillingInvoicesResponse> {
	return api.get<BillingInvoicesResponse>('/api/billing/invoices');
}

/** The org's saved cards as PII-safe metadata only (brand / last4 / expiry —
 *  never a PAN). admin/cfo only. An org never provisioned with the provider — or
 *  an unconfigured / unavailable one — yields an empty list. */
export function getBillingPaymentMethods(): Promise<BillingPaymentMethodsResponse> {
	return api.get<BillingPaymentMethodsResponse>('/api/billing/payment-methods');
}

/** Start a SetupIntent so the org can add or replace a saved card. admin/cfo
 *  only. Returns the provider's single-use `client_secret` (the frontend confirms
 *  the card against it with the provider's JS SDK — no charge, no PAN). An org
 *  never provisioned — or an unconfigured / unavailable provider — yields
 *  `configured=false` + a null secret, never an error. */
export function startBillingSetupIntent(): Promise<BillingSetupIntentResponse> {
	return api.post<BillingSetupIntentResponse>('/api/billing/payment-method/setup-intent', {});
}

/** The sellable plan catalog (active plans, cheapest first) — the plan-change
 *  picker's data source. admin/cfo only. */
export function getBillingPlans(): Promise<BillingPlansResponse> {
	return api.get<BillingPlansResponse>('/api/billing/plans');
}

/** What moving to `planCode` would charge — the provider's own figures, and
 *  nothing is charged or changed. admin/cfo only. A missing card is reported
 *  as `payment_method_required: true`, not refused. Refusals: 404 (unknown
 *  plan / no subscription), 409 `billing_not_configured`, 502. */
export function previewBillingPlanChange(planCode: string): Promise<BillingPlanChangePreview> {
	return api.post<BillingPlanChangePreview>('/api/billing/change-plan/preview', {
		plan_code: planCode
	});
}

/** Confirm the change `preview` priced: this CHARGES what it showed (or less;
 *  AI-read overage accrued up to the change is invoiced with it on top —
 *  decisions §482). admin/cfo only. Idempotent: a change to the plan the org is
 *  already on succeeds with `changed: false`. Coded refusals the dialog
 *  branches on (`types/billing.ts::planChangeCardAction`): 409
 *  `payment_method_required`, 402 `billing_payment_declined` (nothing
 *  changed), 409 `plan_change_preview_stale` (re-preview and confirm again);
 *  also 409 `billing_not_configured` / `plan_change_in_progress`. */
export function changeBillingPlan(preview: BillingPlanChangePreview): Promise<BillingPlanChangeResponse> {
	return api.post<BillingPlanChangeResponse>('/api/billing/change-plan', planChangeRequestFrom(preview));
}

/** Keep the current plan: cancel a move to a free plan that is waiting for the
 *  end of the paid period (decisions §483). Nothing is charged. admin/cfo
 *  only. Idempotent (`cleared: false` when nothing was pending); 409
 *  `scheduled_change_already_due` once the period has ended. */
export function keepCurrentBillingPlan(): Promise<BillingKeepPlanResponse> {
	return api.post<BillingKeepPlanResponse>('/api/billing/scheduled-change/cancel', {});
}

/** Make the card a confirmed SetupIntent saved the org's DEFAULT — the one the
 *  subscription charges. admin/cfo only. The backend checks the SetupIntent
 *  belongs to this org's customer and succeeded; refusals are coded
 *  (`setup_intent_not_found` 404, `setup_intent_incomplete` /
 *  `billing_not_configured` 409, `billing_provider_unavailable` 502). Returns
 *  the refreshed card list, so the caller need not re-fetch. */
export function setDefaultBillingPaymentMethod(
	setupIntentId: string
): Promise<BillingPaymentMethodsResponse> {
	const body: BillingSetDefaultPaymentMethodRequest = { setup_intent_id: setupIntentId };
	return api.post<BillingPaymentMethodsResponse>('/api/billing/payment-method/default', body);
}
