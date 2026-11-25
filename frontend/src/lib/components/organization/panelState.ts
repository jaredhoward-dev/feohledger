/**
 * Field state of the `/organization` panels the page renders through a
 * component (`docs/followups.md`, decisions §205).
 *
 * Each object is a `$state` proxy OWNED BY THE PAGE and handed to its panel
 * with `bind:`, so an unsaved edit lives above the panel boundary and survives
 * a switch to another section and back (the panel unmounts; the page does
 * not). `tests-e2e/organization/section-nav.spec.ts` guards that. A panel
 * never re-seeds these from the server: the page's `loadOrg` does, once.
 */

/** Organization → Company profile. `name` is the org's own name, the rest `settings.company`. */
export interface CompanyFields {
	name: string;
	address: string;
	phone: string;
	website: string;
	taxId: string;
	vatNumber: string;
	companiesHouseNumber: string;
}

/** Organization → Invoice defaults (`settings.invoice_defaults`). */
export interface InvoiceDefaultsFields {
	currency: string;
	/**
	 * The stored default is the user's choice, never re-seeded; marking it
	 * touched once loaded also shows a malformed stored code as an error.
	 */
	currencyTouched: boolean;
	paymentTerms: string;
	numberPrefix: string;
	defaultGl: string;
	defaultCostCenter: string;
}
