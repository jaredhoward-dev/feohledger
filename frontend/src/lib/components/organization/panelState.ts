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

/**
 * Organization → Virtual cards (`settings.cards`). `apiKey` / `clientSecret`
 * are write-only credentials: they start empty and hold only what the admin
 * types (a blank one keeps the stored value).
 */
export interface CardsFields {
	enabled: boolean;
	programType: string;
	provider: string;
	region: string;
	apiKey: string;
	clientId: string;
	clientSecret: string;
	customerHashId: string;
	walletHashId: string;
	expiryDays: number;
	sandbox: boolean;
}

// `value` is the stored card-program region (the backend keeps `UK`, not
// ISO's `GB`); `iso` is what Intl names in the reader's language.
export const CARD_REGIONS = [
	{ value: 'US', iso: 'US', eu: false, default_provider: 'lithic' },
	{ value: 'UK', iso: 'GB', eu: false, default_provider: 'lithic' },
	{ value: 'DE', iso: 'DE', eu: true, default_provider: 'lithic' },
	{ value: 'FR', iso: 'FR', eu: true, default_provider: 'lithic' },
	{ value: 'NL', iso: 'NL', eu: true, default_provider: 'lithic' },
	{ value: 'ZA', iso: 'ZA', eu: false, default_provider: 'nium' },
	{ value: 'AU', iso: 'AU', eu: false, default_provider: 'nium' },
	{ value: 'SG', iso: 'SG', eu: false, default_provider: 'nium' },
	{ value: 'HK', iso: 'HK', eu: false, default_provider: 'nium' },
	{ value: 'IN', iso: 'IN', eu: false, default_provider: 'nium' },
	{ value: 'CA', iso: 'CA', eu: false, default_provider: 'nium' },
	{ value: 'AE', iso: 'AE', eu: false, default_provider: 'nium' },
	{ value: 'JP', iso: 'JP', eu: false, default_provider: 'nium' }
] as const;

/** The card provider a region gets when the admin leaves the choice on auto. */
export function cardAutoProvider(region: string): string {
	return CARD_REGIONS.find((r) => r.value === region)?.default_provider ?? 'nium';
}
