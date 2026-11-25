import { m } from '#lib/i18n/store.svelte.ts';
import type { PaymentExecutionMode } from '#lib/types/paymentExecution.ts';

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

/** Organization → Branding (white-label, `settings.brand`). See docs/white-label.md. */
export interface BrandingFields {
	productName: string;
	logoUrl: string;
	accentColor: string;
	accentStrongColor: string;
	supportUrl: string;
	legalUrl: string;
	/**
	 * Per-tenant base URL stamped into outbound emails (approval links, portal
	 * invites, signup confirmations). Empty ⇒ the platform default. `{slug}` in
	 * the value is optional: substituted when present, used verbatim when not.
	 * See docs/white-label.md § Tenant URL.
	 */
	tenantUrlTemplate: string;
	/**
	 * Deliberately a SEPARATE field from the one above: this value is
	 * registered at the customer's IdP, so changing it is an operator-sequenced
	 * migration (add the new URI at the IdP first, verify a real login, then
	 * remove the old one) rather than a preference. Folding the two would mean
	 * fixing invite links silently breaks SSO.
	 */
	ssoCallbackBaseUrl: string;
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

/**
 * Organization → AI extraction (`settings.extraction`). `apiKey` / `awsSecret`
 * are write-only credentials, like every SecretField. `keyInvalid` marks the
 * key field the page refused a provider switch for.
 */
export interface ExtractionFields {
	programType: string;
	provider: string;
	apiKey: string;
	awsKeyId: string;
	awsSecret: string;
	awsRegion: string;
	ollamaUrl: string;
	ollamaModel: string;
	keyInvalid: boolean;
}

// Product names, not copy, except Ollama's "(local)" note, which is keyed
// (`extractionProviderLabel`).
export const EXTRACTION_PROVIDERS = [
	{ value: 'claude_vision', name: 'Claude Vision (Anthropic)' },
	{ value: 'openai_vision', name: 'GPT-4V (OpenAI)' },
	{ value: 'aws_textract', name: 'AWS Textract' },
	{ value: 'ollama', name: 'Ollama' }
];

export const extractionProviderLabel = (provider: string): string =>
	provider === 'ollama'
		? m('org.extraction.providerOllama')
		: (EXTRACTION_PROVIDERS.find((p) => p.value === provider)?.name ?? provider);

/** Organization → Fraud detection: the composed rule set (`settings.fraud_rules` over the defaults). */
export interface FraudRules {
	round_amount_enabled: boolean;
	future_date_enabled: boolean;
	bank_change_enabled: boolean;
	stat_anomaly_enabled: boolean;
	rush_payment_enabled: boolean;
	new_vendor_large_enabled: boolean;
	personal_email_enabled: boolean;
	llm_anomaly_enabled: boolean;
	round_amount_min: string;
	rush_payment_max_days: number;
	new_vendor_max_age_days: number;
	new_vendor_large_amount: string;
	stat_anomaly_sigma: number;
	stat_anomaly_min_history: number;
	personal_email_domains: string[];
}

/**
 * Organization → Payments (`settings.payments`). `apiKey` / `webhookSecret`
 * are write-only credentials, like every SecretField.
 */
export interface PaymentsFields {
	mode: PaymentExecutionMode;
	provider: string;
	apiKey: string;
	orgId: string;
	originatingAccount: string;
	webhookSecret: string;
	sandbox: boolean;
	cfoThreshold: number | null;
}

/**
 * Organization → Payments → the record-only NACHA bank file
 * (`settings.payments.nacha`, issue #517). The offset account number reads
 * back masked, so `offsetAccount` starts blank; saving it blank keeps the
 * stored number (decisions §432).
 */
export interface NachaFields {
	companyName: string;
	companyId: string;
	odfiRouting: string;
	bankName: string;
	offsetRouting: string;
	offsetAccount: string;
	offsetAccountType: string;
}
