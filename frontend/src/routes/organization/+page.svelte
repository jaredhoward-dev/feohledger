<script lang="ts">
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { page } from '$app/state';
	import { api, ApiError } from '#lib/api.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import SecretReveal from '#lib/components/ui/SecretReveal.svelte';
	import { CREDENTIAL_BLOCK_UNOPENABLE } from '#lib/api/codedRefusals.ts';
	import {
		getProviderCredentials,
		updateProviderCredentials
	} from '#lib/api/providerCredentials.ts';
	import {
		credentialUpdate,
		type CredentialBlock,
		type ProviderCredentialStatus
	} from '#lib/types/providerCredentials.ts';
	import SettingsRail from '#lib/components/ui/SettingsRail.svelte';
	import { normalizeCurrencyCode, currencyCodeProblem } from '#lib/utils/currencyCode.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { orgCurrency } from '#lib/stores/orgSettings.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import {
		getChatNotifications,
		revokeChatWebhook,
		rotateChatWebhook,
		updateChatNotifications
	} from '#lib/api/chatNotifications.ts';
	import { getEmailIntake, rotateEmailIntakeToken } from '#lib/api/emailIntake.ts';
	import {
		CHAT_EVENT_LABELS,
		CHAT_PROVIDER_LABELS,
		type ChatNotificationStatus
	} from '#lib/types/chatNotifications.ts';
	import { getSsoSettings, updateSsoSettings } from '#lib/api/ssoSettings.ts';
	import { ssoRefusalText } from '#lib/api/ssoRefusal.ts';
	import type { SsoSettingsStatus } from '#lib/types/ssoSettings.ts';
	import { formatList } from '#lib/utils/list.ts';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { errorMessage } from '#lib/utils/apiError.ts';
	import { setupStepDone, setupStepHref, type SetupStepId } from '#lib/utils/onboarding.ts';
	import type { OnboardingResponse } from '#lib/types/analytics.ts';
	import ErpConnectionPanel from '#lib/components/organization/ErpConnectionPanel.svelte';
	import CommerceConnectionPanel from '#lib/components/organization/CommerceConnectionPanel.svelte';
	import ScimTokenPanel from '#lib/components/organization/ScimTokenPanel.svelte';
	import CloseWorkspacePanel from '#lib/components/organization/CloseWorkspacePanel.svelte';
	import LineTotalCheckPanel from '#lib/components/organization/LineTotalCheckPanel.svelte';
	import MatchRulesPanel from '#lib/components/organization/MatchRulesPanel.svelte';
	import AgentAutonomyPanel from '#lib/components/organization/AgentAutonomyPanel.svelte';
	import CompanyProfilePanel from '#lib/components/organization/CompanyProfilePanel.svelte';
	import ResidencyPanel from '#lib/components/organization/ResidencyPanel.svelte';
	import SsoPanel from '#lib/components/organization/SsoPanel.svelte';
	import PaymentsPanel from '#lib/components/organization/PaymentsPanel.svelte';
	import FraudRulesPanel from '#lib/components/organization/FraudRulesPanel.svelte';
	import ExtractionPanel from '#lib/components/organization/ExtractionPanel.svelte';
	import BrandingPanel from '#lib/components/organization/BrandingPanel.svelte';
	import CardsPanel from '#lib/components/organization/CardsPanel.svelte';
	import SecurityPanel from '#lib/components/organization/SecurityPanel.svelte';
	import PlanPanel from '#lib/components/organization/PlanPanel.svelte';
	import DataSyncPanel from '#lib/components/organization/DataSyncPanel.svelte';
	import GettingStartedPanel from '#lib/components/organization/GettingStartedPanel.svelte';
	import InvoiceDefaultsPanel from '#lib/components/organization/InvoiceDefaultsPanel.svelte';
	import {
		cardAutoProvider,
		extractionProviderLabel,
		type BrandingFields,
		type CardsFields,
		type CompanyFields,
		type ExtractionFields,
		type NachaFields,
		type PaymentsFields,
		type ResidencyAlignment,
		type SsoFields,
		type FraudRules,
		type InvoiceDefaultsFields
	} from '#lib/components/organization/panelState.ts';
	import { withoutOwnedFraudKeys } from '#lib/types/orgControls.ts';
	import type { StoredErpConfig } from '#lib/types/erpConnection.ts';
	import {
		FEATURE_ERP_INTEGRATIONS,
		FEATURE_ERP_MERGE
	} from '#lib/types/planFeatures.ts';
	import {
		NACHA_FIELD_LABEL_KEYS,
		nachaForSave,
		nachaProblems
	} from '#lib/types/paymentExecution.ts';

	interface CompanyProfile {
		address: string;
		phone: string;
		website: string;
		tax_id: string;
		logo_url: string;
		vat_registration_number?: string;
		companies_house_number?: string;
	}

	interface InvoiceDefaults {
		currency: string;
		payment_terms: string;
		number_prefix: string;
		default_gl_account: string;
		default_cost_center: string;
	}

	interface OrgSettings {
		company: CompanyProfile;
		invoice_defaults: InvoiceDefaults;
		erp?: StoredErpConfig;
		fraud_rules?: Partial<FraudRules>;
	}

	interface OrgResponse {
		id: string;
		name: string;
		slug: string;
		plan: string;
		settings: OrgSettings;
		created_at: string;
		// The server's own resolved reporting currency (all four rungs of
		// `currency_conversion.resolve_reporting_currency`, admin or not). This
		// page doesn't render it directly — it reloads the `orgCurrency` store
		// after a save instead (see `orgCurrency.reset()` below) — but the field
		// is always present, so the type stays honest with the wire shape.
		resolved_reporting_currency: string;
	}

	let org = $state<OrgResponse | null>(null);
	// Set when `GET /api/organization` fails, so the page says so and offers a
	// retry. `org` is only ever set on success, so without this the template's
	// fallback branch read "Loading…" forever after a failed load (#635).
	let orgLoadFailed = $state(false);
	// Editable fields
	// Each panel's fields are one object, owned here and bound into the panel
	// component, so an unsaved edit survives a panel switch (`panelState.ts`).
	let company = $state<CompanyFields>({
		name: '',
		address: '',
		phone: '',
		website: '',
		taxId: '',
		vatNumber: '',
		companiesHouseNumber: ''
	});
	let invoiceDefaults = $state<InvoiceDefaultsFields>({
		currency: 'USD',
		currencyTouched: false,
		paymentTerms: 'Net 30',
		numberPrefix: 'INV-',
		defaultGl: '',
		defaultCostCenter: ''
	});
	// Provider credentials are write-only (`/api/organization/credentials`). Each
	// secret input on the payments, cards and extraction panels (the ERP panel keeps its own,
	// `ErpConnectionPanel`) starts EMPTY and holds
	// only what the admin types — a blank one keeps the stored value. `creds`
	// lists which paths the server holds (names only); `secretClear` is each
	// field's "remove" toggle, keyed `block.path`.
	let creds = $state<ProviderCredentialStatus | null>(null);
	// Every key a `SecretField` binds is present from the start: Svelte 5 refuses
	// `bind:` of `undefined` to a prop that declares a fallback.
	let secretClear = $state<Record<string, boolean>>({
		'payments.api_key': false,
		'payments.webhook_secret': false,
		'cards.api_key': false,
		'cards.client_secret': false,
		'extraction.api_key': false,
		'extraction.aws_secret_access_key': false
	});
	const isStored = (block: CredentialBlock, path: string): boolean =>
		creds?.[block].includes(path) ?? false;

	async function loadCredentials() {
		try {
			creds = await getProviderCredentials();
		} catch {
			// Non-fatal: the fields then say nothing is stored, and a blank field
			// still keeps whatever is — the server decides, not this flag.
		}
	}

	/**
	 * Save one block's typed secrets / remove toggles, after its configuration
	 * PATCH. No request when nothing secret changed; throws (for the caller's
	 * toast) when the server refuses, leaving the typed values in place.
	 */
	async function saveSecrets(block: CredentialBlock, typed: Record<string, string>): Promise<void> {
		const body = credentialUpdate(
			Object.fromEntries(
				Object.entries(typed).map(([path, value]) => [
					path,
					{ value, clear: secretClear[`${block}.${path}`] ?? false }
				])
			)
		);
		if (!body) return;
		creds = await updateProviderCredentials(block, body);
		for (const path of Object.keys(typed)) secretClear[`${block}.${path}`] = false;
	}
	// A block whose stored value can no longer be opened: every save of it
	// answers 503 `credential_block_unopenable` (the server must open it to
	// merge), so its panel offers clearing it instead (`CredentialResetPrompt`,
	// decisions §464). Set from the refusal's own `params.block` — a PATCH that
	// drops a rail's keys names the block it could not open.
	let unopenable = $state<Partial<Record<CredentialBlock, boolean>>>({});
	function noteUnopenable(err: unknown): void {
		if (!(err instanceof ApiError) || err.code !== CREDENTIAL_BLOCK_UNOPENABLE) return;
		const block = err.params.block;
		if (block === 'payments' || block === 'cards' || block === 'extraction') {
			unopenable[block] = true;
		}
	}
	function onCredentialsCleared(block: CredentialBlock, status: ProviderCredentialStatus): void {
		creds = status;
		unopenable[block] = false;
		for (const key of Object.keys(secretClear)) {
			if (key.startsWith(`${block}.`)) secretClear[key] = false;
		}
	}
	// Extraction
	let extractionFields = $state<ExtractionFields>({
		programType: 'platform',
		provider: 'claude_vision',
		apiKey: '',
		awsKeyId: '',
		awsSecret: '',
		awsRegion: 'us-east-1',
		ollamaUrl: 'http://localhost:11434',
		ollamaModel: 'llama3.2-vision:11b',
		keyInvalid: false
	});
	let savingExtraction = $state(false);
	// The provider the stored key belongs to — as loaded, then as last saved
	// (`null`: the block names none). A save that changes it drops the sealed
	// key server-side (`extraction_secrets_to_drop`, decisions §444), so the
	// key field reads "stored" only while the dropdown still shows this one.
	let extractionSavedProvider = $state<string | null>(null);
	const extractionProviderChanged = $derived(extractionFields.provider !== extractionSavedProvider);
	const extractionKeyStored = (path: string): boolean =>
		!extractionProviderChanged && isStored('extraction', path);
	// A key is stored for the saved provider, and this save would drop it.
	const extractionSwitchDropsKey = $derived(
		extractionProviderChanged &&
			(isStored('extraction', 'api_key') || isStored('extraction', 'aws_secret_access_key'))
	);
	/** The secret path a provider needs, or `null` (Ollama runs keyless). */
	const extractionSecretPath = (provider: string): 'api_key' | 'aws_secret_access_key' | null =>
		provider === 'aws_textract'
			? 'aws_secret_access_key'
			: provider === 'claude_vision' || provider === 'openai_vision'
				? 'api_key'
				: null;
	let testingExtraction = $state(false);
	let extractionTestResult = $state<{ success: boolean; message: string } | null>(null);

	async function testExtraction() {
		testingExtraction = true;
		extractionTestResult = null;
		try {
			extractionTestResult = await api.post<{ success: boolean; message: string }>('/api/organization/test-extraction', {
				provider: extractionFields.programType === 'platform' ? 'claude_vision' : extractionFields.provider,
				api_key: extractionFields.apiKey,
				aws_access_key_id: extractionFields.awsKeyId,
				aws_secret_access_key: extractionFields.awsSecret,
				aws_region: extractionFields.awsRegion,
				base_url: extractionFields.ollamaUrl,
				model: extractionFields.ollamaModel,
			});
		} catch (err) {
			extractionTestResult = { success: false, message: errorMessage(err, m('org.toast.testFailed')) };
		} finally {
			testingExtraction = false;
		}
	}

	// Cards
	let cardsFields = $state<CardsFields>({
		enabled: false,
		programType: 'platform',
		provider: '',
		region: 'US',
		apiKey: '',
		clientId: '',
		clientSecret: '',
		customerHashId: '',
		walletHashId: '',
		expiryDays: 30,
		sandbox: true
	});
	let savingCards = $state(false);
	// ── Read-only mode for non-admins ───────────────────────────────────
	// Every mutating endpoint behind this page is `require_roles(ROLE_ADMIN)`
	// — the server is and stays the authority. What was missing was any
	// client-side acknowledgement of that: a clerk navigating straight here got
	// a fully editable form across ~10 panels whose every Save 403s, which
	// reads as a broken product rather than a permission boundary.
	//
	// `auth.user` starts null while `GET /api/auth/me` is in flight, and
	// `isAdmin` is false until it lands — so gate on the user having RESOLVED,
	// otherwise the banner flashes on every admin's page load and the form is
	// briefly disabled under them.
	const userLoaded = $derived(auth.user !== null);
	const readOnly = $derived(userLoaded && !auth.isAdmin);
	// Plan gates (decisions §253/§258). The server refuses each with a 402
	// (`plan_feature_required`) whatever this page shows; these only swap a
	// control that would be refused for the upgrade prompt. What a save would
	// turn OFF is never gated, so a downgraded tenant can still switch SSO or
	// "require SSO" off.
	const erpEntitled = $derived(auth.hasFeature(FEATURE_ERP_INTEGRATIONS));
	const erpMergeEntitled = $derived(auth.hasFeature(FEATURE_ERP_MERGE));

	// ── Section navigation ────────────────────────────────────────────────
	// Fifteen panels' worth of unrelated concerns — company identity, DNS, chat
	// webhooks, an AI model choice, ACH rails, fraud thresholds, the billing
	// plan — share this route because they share `PATCH /api/organization`.
	// Stacked on one scroll that made the page a book with no contents page: ten
	// of the fifteen were reachable only by scrolling and reading. So one panel
	// shows at a time, picked from `ui/SettingsRail.svelte` and addressed by
	// `?section=`, the same URL-backed shape `/admin?tab=` already uses (and
	// which `lib/nav.ts` already deep-links into).
	//
	// Field state stays here, at page level, deliberately: only the MARKUP is
	// conditional, so an unsaved edit in one panel survives a trip to another
	// and is still there — with its Save button — on the way back.
	//
	// A slug is part of the URL contract: it lands in bookmarks, in the
	// Getting-started links below, and in the docs. Renaming one breaks those.
	type SectionGroup = {
		/** Omitted for the lead-in panel, which stands outside the grouping. */
		labelKey?: MessageKey;
		items: { slug: string; labelKey: MessageKey }[];
	};

	const SECTION_GROUPS: SectionGroup[] = [
		{ items: [{ slug: 'getting-started', labelKey: 'org.gettingStarted.title' }] },
		{
			labelKey: 'org.rail.group.company',
			items: [
				{ slug: 'company', labelKey: 'org.section.company' },
				{ slug: 'defaults', labelKey: 'org.section.defaults' },
				{ slug: 'branding', labelKey: 'org.section.branding' },
				{ slug: 'custom-domains', labelKey: 'org.section.customDomains' }
			]
		},
		{
			labelKey: 'org.rail.group.integrations',
			items: [
				{ slug: 'erp', labelKey: 'org.section.erp' },
				{ slug: 'commerce', labelKey: 'org.section.commerce' },
				{ slug: 'extraction', labelKey: 'org.section.extraction' },
				{ slug: 'email-intake', labelKey: 'org.section.emailIntake' },
				{ slug: 'chat', labelKey: 'org.section.chat' },
				{ slug: 'data-sync', labelKey: 'org.section.dataSync' }
			]
		},
		{
			labelKey: 'org.rail.group.money',
			items: [
				// The rail's own short label: the panel heading is "Payments (ACH /
				// Wire / RTP)", which does not sit in a 200px column.
				{ slug: 'payments', labelKey: 'org.rail.payments' },
				{ slug: 'cards', labelKey: 'org.section.cards' },
				// Its own audited writer (decisions §512).
				{ slug: 'match-rules', labelKey: 'org.section.matchRules' }
			]
		},
		{
			labelKey: 'org.rail.group.compliance',
			items: [
				{ slug: 'security', labelKey: 'org.section.security' },
				{ slug: 'sso', labelKey: 'orgSso.title' },
				{ slug: 'scim', labelKey: 'org.section.scim' },
				{ slug: 'fraud', labelKey: 'org.section.fraud' },
				// Their own audited writers (decisions §502).
				{ slug: 'line-total-check', labelKey: 'org.section.lineTotal' },
				{ slug: 'exception-agents', labelKey: 'org.section.agentAutonomy' },
				{ slug: 'residency', labelKey: 'org.section.dataResidency' }
			]
		},
		{
			labelKey: 'org.rail.group.account',
			items: [
				{ slug: 'plan', labelKey: 'org.section.plan' },
				// Deleting an unused workspace, which frees its addresses
				// (decisions §306). Last: the one irreversible panel.
				{ slug: 'close', labelKey: 'org.section.close' }
			]
		}
	];

	const SECTION_SLUGS = new Set(SECTION_GROUPS.flatMap((g) => g.items.map((i) => i.slug)));
	const DEFAULT_SECTION = 'getting-started';

	// The five anchors the page was navigated by before it had panels. They live
	// in the Getting-started card, and outside this repo in whatever bookmarks
	// and links people already hold, so they have to keep working.
	const LEGACY_HASH_SECTIONS: Record<string, string> = {
		'org-company': 'company',
		'org-defaults': 'defaults',
		'org-branding': 'branding',
		'org-email-intake': 'email-intake',
		'org-payments': 'payments'
	};

	// An unknown slug falls back rather than rendering an empty page — the same
	// treatment `/admin` gives a stale `?tab=`.
	//
	// A legacy anchor is resolved HERE, by derivation, rather than by rewriting
	// the URL to `?section=` in an effect. Rewriting was the first version and is
	// the wrong shape for this: `replaceState` throws if it runs before the
	// SvelteKit router has initialised, and an effect on first hydration is
	// exactly that window — so the tidier URL came at the cost of a failure mode
	// on the one path whose whole job is to still work for a link someone saved
	// a year ago. Deriving it cannot fail, needs no import, and the stale `#hash`
	// clears itself on the first rail click (`SettingsRail`'s hrefs are
	// query-only). Precedence is query-before-hash so an explicit `?section=`
	// always wins over an anchor that happens to be along for the ride.
	const section = $derived.by(() => {
		const slug = page.url.searchParams.get('section');
		if (slug && SECTION_SLUGS.has(slug)) return slug;
		return LEGACY_HASH_SECTIONS[page.url.hash.replace(/^#/, '')] ?? DEFAULT_SECTION;
	});

	const railGroups = $derived(
		SECTION_GROUPS.map((g) => ({
			label: g.labelKey ? m(g.labelKey) : undefined,
			items: g.items.map((i) => ({ slug: i.slug, label: m(i.labelKey) }))
		}))
	);

	// The page's EAGER read. `GET /api/organization` alone fills the company,
	// defaults, ERP, cards, security, fraud-override, payments and extraction
	// forms plus the plan card, and the whole template is gated on it, so it has
	// no panel to be lazy about.
	//
	// Every other read belongs to exactly one panel and now waits for it — seven
	// requests on arrival became two, and an admin who came to change the ACH
	// cut-off no longer pays for a DNS lookup, a residency read and a chat-webhook
	// probe on the way. Each is `once()`-guarded so returning to a panel does not
	// refetch, and the three admin-only ones keep their own role gate: a doomed
	// request is not a way to find out what your role is.
	$effect(() => {
		loadOrg();
		// `payments.cfo_approval_above` is a bare number denominated in the org's
		// reporting currency (`payment_controls.cfo_approval_decision`), so the
		// field names that currency instead of hardcoding a dollar sign.
		orgCurrency.ensureLoaded();
	});

	/**
	 * Panel-scoped loads, fired the first time their panel is shown.
	 *
	 * `requestedPanels` is keyed by panel rather than by a per-loader boolean so the
	 * set is visible in one place; a loader that also needs `auth` keeps its own
	 * effect below, because folding those in would make every panel's read
	 * re-run the moment `/me` resolves.
	 */
	const requestedPanels = new Set<string>();

	/**
	 * Getting started — the settings a new workspace configures first, each
	 * ticked from the same server-side facts as the dashboard's setup checklist
	 * (`GET /api/dashboard/onboarding`, `docs/decisions.md` §315). Read again
	 * each time the panel is shown, so a step saved on another panel is ticked
	 * on the way back. Admin-only: a non-admin, or a read that fails, gets the
	 * same links with no done state at all — an unknown is never drawn as
	 * unfinished.
	 */
	let onboarding = $state<OnboardingResponse | null>(null);
	const GETTING_STARTED: { id: SetupStepId; labelKey: MessageKey }[] = [
		{ id: 'company', labelKey: 'org.gettingStarted.company' },
		{ id: 'defaults', labelKey: 'org.gettingStarted.defaults' },
		{ id: 'users', labelKey: 'org.gettingStarted.users' },
		{ id: 'security', labelKey: 'org.gettingStarted.security' },
		{ id: 'erp', labelKey: 'org.gettingStarted.erp' },
		{ id: 'vendors', labelKey: 'org.gettingStarted.vendors' },
		// Invoice approval thresholds (auto-approve, CFO above, maximum) live on
		// the workflow's approval step, not on this page; the Payments panel's
		// only threshold is the CFO sign-off for a payment RUN, which is its own
		// step (#635).
		{ id: 'workflow', labelKey: 'org.gettingStarted.approvals' },
		{ id: 'payments', labelKey: 'org.gettingStarted.paymentRunSignoff' },
		{ id: 'email_intake', labelKey: 'org.gettingStarted.emailIntake' },
		{ id: 'branding', labelKey: 'org.gettingStarted.branding' }
	];
	const gettingStartedSteps = $derived(
		GETTING_STARTED.map(({ id, labelKey }) => ({
			id,
			label: m(labelKey),
			href: setupStepHref(id, '/organization'),
			done: setupStepDone(id, onboarding)
		}))
	);
	$effect(() => {
		if (section !== 'getting-started' || !userLoaded || !auth.isAdmin) return;
		api.get<OnboardingResponse>('/api/dashboard/onboarding').then(
			(res) => (onboarding = res),
			() => (onboarding = null)
		);
	});

	function once(slug: string, load: () => void) {
		if (section !== slug || requestedPanels.has(slug)) return;
		requestedPanels.add(slug);
		load();
	}

	$effect(() => {
		// `/api/public-config` (public) and
		// `/api/organization/branding/custom-domains` + `/data-residency` (both
		// `get_current_user`) answer a clerk with real tenant data, so these three
		// need no role gate — only their panel.
		once('branding', loadPlatformTenantUrl);
		once('custom-domains', loadCustomDomains);
		once('residency', loadResidency);
	});

	async function loadOrg() {
		orgLoadFailed = false;
		try {
			const data = await api.get<OrgResponse>('/api/organization');
			org = data;
			company = {
				name: data.name,
				address: data.settings.company.address,
				phone: data.settings.company.phone,
				website: data.settings.company.website,
				taxId: data.settings.company.tax_id,
				vatNumber: data.settings.company.vat_registration_number ?? '',
				companiesHouseNumber: data.settings.company.companies_house_number ?? ''
			};
			invoiceDefaults = {
				currency: data.settings.invoice_defaults.currency,
				currencyTouched: true,
				paymentTerms: data.settings.invoice_defaults.payment_terms,
				numberPrefix: data.settings.invoice_defaults.number_prefix,
				defaultGl: data.settings.invoice_defaults.default_gl_account,
				defaultCostCenter: data.settings.invoice_defaults.default_cost_center
			};
			// Cards
			const cards = (data.settings as unknown as Record<string, unknown>).cards as Record<string, unknown> | undefined;
			if (cards) {
				cardsFields.enabled = (cards.enabled as boolean) ?? false;
				cardsFields.programType = (cards.program_type as string) || 'platform';
				cardsFields.provider = (cards.provider as string) || '';
				cardsFields.region = (cards.region as string) || 'US';
				cardsFields.clientId = (cards.client_id as string) || '';
				cardsFields.customerHashId = (cards.customer_hash_id as string) || '';
				cardsFields.walletHashId = (cards.wallet_hash_id as string) || '';
				cardsFields.expiryDays = (cards.default_expiry_days as number) || 30;
				// A stored block with no `sandbox` key runs LIVE: the card adapters
				// read `config.get("sandbox", False)`. Showing it as testing made the
				// next save silently move the block to sandbox (and, since §464,
				// drop its keys). A block never configured keeps the declared
				// default above.
				cardsFields.sandbox = typeof cards.sandbox === 'boolean' ? cards.sandbox : false;
			}
			// Security (MFA enforcement)
			const mfaCfg = (data.settings as unknown as Record<string, unknown>).mfa as
				| Record<string, unknown>
				| undefined;
			mfaRequired = (mfaCfg?.required as boolean) ?? false;

			// Fraud rules — this read carries only the org's OVERRIDES. The
			// canonical defaults they layer onto come from an admin-only endpoint
			// fetched in its own role-gated effect, so the two halves can land in
			// either order and whichever arrives second composes the form.
			fraudOverrides =
				((data.settings as unknown as Record<string, unknown>).fraud_rules as
					| Partial<FraudRules>
					| undefined) ?? {};
			composeFraud();
			// Payments
			const pmt = (data.settings as unknown as Record<string, unknown>).payments as
				| Record<string, unknown>
				| undefined;
			if (pmt) {
				loadPaymentsExtras(pmt);
				paymentsFields.provider = (pmt.provider as string) || 'mock';
				paymentsProgramType = (pmt.program_type as string) || 'byok';
				paymentsFields.orgId = (pmt.org_id as string) || '';
				paymentsFields.originatingAccount = (pmt.originating_account_id as string) || '';
				paymentsFields.sandbox = (pmt.sandbox as boolean) ?? true;
				paymentsFields.cfoThreshold = (pmt.cfo_approval_above as number | null) ?? null;
			}
			// Extraction
			const extraction = (data.settings as unknown as Record<string, unknown>).extraction as Record<string, unknown> | undefined;
			if (extraction) {
				extractionFields.programType = (extraction.program_type as string) || 'platform';
				extractionFields.provider = (extraction.provider as string) || 'claude_vision';
				extractionSavedProvider =
					typeof extraction.provider === 'string' && extraction.provider.trim()
						? extraction.provider.trim()
						: null;
				// The BYOK key / AWS secret are sealed and never read back: their
				// fields start empty ("leave blank to keep"), like every SecretField.
				extractionFields.awsKeyId = (extraction.aws_access_key_id as string) || '';
				extractionFields.awsRegion = (extraction.aws_region as string) || 'us-east-1';
				extractionFields.ollamaUrl = (extraction.base_url as string) || 'http://localhost:11434';
				extractionFields.ollamaModel = (extraction.model as string) || 'llama3.2-vision:11b';
			}
			// Branding (white-label)
			const brandCfg = (data.settings as unknown as Record<string, unknown>).brand as
				| Record<string, unknown>
				| undefined;
			if (brandCfg) {
				branding.productName = (brandCfg.product_name as string) || '';
				branding.logoUrl = (brandCfg.logo_url as string) || '';
				branding.accentColor = (brandCfg.accent_color as string) || '';
				branding.accentStrongColor = (brandCfg.accent_strong_color as string) || '';
				branding.supportUrl = (brandCfg.support_url as string) || '';
				branding.legalUrl = (brandCfg.legal_url as string) || '';
				branding.tenantUrlTemplate = (brandCfg.tenant_url_template as string) || '';
				branding.ssoCallbackBaseUrl = (brandCfg.sso_callback_base_url as string) || '';
			}
		} catch {
			// In place, not a toast: the toast faded and left the loading line.
			orgLoadFailed = true;
		}
	}

	let savingProfile = $state(false);
	let savingDefaults = $state(false);

	// Security
	let mfaRequired = $state(false);
	let savingSecurity = $state(false);
	// Advisory, computed server-side on every read (never persisted) — whether
	// "require MFA" is actually enforced right now, or a silent no-op because
	// the platform master switch (FEOH_MFA_ENABLED) is off. Derived off `org`
	// (not a plain load-time assignment) so it stays correct after
	// `saveSecurity()` replaces `org` with the PATCH response too. See
	// backend/app/api/organization.py's `_org_response`.
	let mfaEnforcementActive = $derived(
		((org?.settings as unknown as Record<string, unknown> | undefined)?.mfa as
			| Record<string, unknown>
			| undefined)?.enforcement_active === true
	);

	// Fraud detection — defaults loaded once from the backend; the form
	// reflects (defaults ⊕ org overrides) so a stale UI can't drift from
	// what the warning engine actually evaluates.
	let fraudDefaults = $state<FraudRules | null>(null);
	let fraud = $state<FraudRules | null>(null);
	let personalEmailDomainsText = $state('');
	let savingFraud = $state(false);
	// The org's stored overrides, held on their own because the two halves of
	// this form arrive from two endpoints with two different gates: the overrides
	// ride the role-open `GET /api/organization`, the defaults come from an
	// admin-only route. `null` is "the org read has not landed", which is NOT the
	// same fact as `{}` ("this org overrides nothing").
	let fraudOverrides = $state<Partial<FraudRules> | null>(null);

	/** Compose the form from (defaults ⊕ overrides), once both have landed. */
	function composeFraud() {
		if (!fraudDefaults || fraudOverrides === null) return;
		fraud = { ...fraudDefaults, ...fraudOverrides };
		personalEmailDomainsText = fraud.personal_email_domains.join('\n');
	}

	$effect(() => {
		// `GET /api/organization/fraud-rules/defaults` is admin-only, so this is
		// gated on the role like the chat and email-intake reads rather than fired
		// and swallowed. For a non-admin the panel renders the admin-only hint, so
		// the request was not merely refused — it was pointless.
		//
		// `fraudDefaults` is its own already-loaded guard, so this one needs no
		// `once()` — the panel can be revisited without refetching.
		if (section !== 'fraud' || !userLoaded || !auth.isAdmin || fraudDefaults) return;
		loadFraudDefaults();
	});

	$effect(() => {
		// Admin-only, like the fraud defaults: a clerk sees the read-only banner
		// and has no secret field to describe.
		if (!userLoaded || !auth.isAdmin || creds) return;
		loadCredentials();
	});

	async function loadFraudDefaults() {
		try {
			fraudDefaults = await api.get<FraudRules>('/api/organization/fraud-rules/defaults');
			composeFraud();
		} catch {
			// Non-fatal, and deliberately not substituted: a hardcoded client-side
			// default would render switches claiming a rule set nobody configured,
			// beside a Reset button with nothing true to reset to. The panel stays
			// absent instead.
		}
	}

	// Payments
	// `mode` starts at "processor": see the execution-mode note below.
	let paymentsFields = $state<PaymentsFields>({
		mode: 'processor',
		provider: 'mock',
		apiKey: '',
		orgId: '',
		originatingAccount: '',
		webhookSecret: '',
		sandbox: true,
		cfoThreshold: null
	});
	let paymentsProgramType = $state('byok'); // mock = no key; modern_treasury = byok keys
	let savingPayments = $state(false);

	// --- Payment execution mode + NACHA bank file (issue #517) ----------------
	// `mode` absent means "processor" by default, but the backend still forces
	// record-only on a deployed tenant with no real processor
	// (`services/payment_execution_mode`) — so an absent mode is re-sent absent
	// unless the admin changes it, rather than written as an explicit choice
	// nobody made. An unknown stored value shows as record-only, which is what
	// the server resolves it to (fail closed).
	let paymentsModeStored = $state<string | null>(null);
	let nachaFields = $state<NachaFields>({
		companyName: '',
		companyId: '',
		odfiRouting: '',
		bankName: '',
		offsetRouting: '',
		offsetAccount: '',
		offsetAccountType: ''
	});
	// The balanced-file offset account (decisions §432). The account number
	// reads back masked (`****1234`), so the input starts blank with the mask as
	// its placeholder; saving it blank keeps the stored number.
	let nachaOffsetAccountMask = $state('');
	// The stored offset's bank, while an account number is on file: a blank
	// account is "keep it" only at that same bank (`nachaProblems`).
	let nachaOffsetOnFile = $state<{ offset_routing: string; offset_account_type: string } | null>(
		null
	);
	function rememberNachaOnFile(block: Record<string, unknown> | null | undefined): void {
		nachaOffsetOnFile =
			typeof block?.offset_account === 'string' && block.offset_account !== ''
				? {
						offset_routing: typeof block.offset_routing === 'string' ? block.offset_routing : '',
						offset_account_type:
							typeof block.offset_account_type === 'string' ? block.offset_account_type : ''
					}
				: null;
	}
	// Every OTHER key of the stored `settings.payments` block. The save is a
	// shallow replace of the whole block, so a key this form does not manage —
	// `require_run_segregation`, `home_currency`, anything newer than this
	// build — would be erased by a save that did not carry it back.
	let paymentsExtras = $state<Record<string, unknown>>({});

	const PAYMENTS_FORM_KEYS = new Set([
		'provider',
		'program_type',
		'api_key',
		'org_id',
		'originating_account_id',
		'webhook_secret',
		'sandbox',
		'cfo_approval_above',
		'mode',
		'nacha'
	]);

	function loadPaymentsExtras(pmt: Record<string, unknown>) {
		paymentsExtras = Object.fromEntries(
			Object.entries(pmt).filter(([key]) => !PAYMENTS_FORM_KEYS.has(key))
		);
		const mode = pmt.mode;
		paymentsModeStored = typeof mode === 'string' ? mode : null;
		paymentsFields.mode =
			paymentsModeStored === null || paymentsModeStored === 'processor' ? 'processor' : 'record_only';
		const nacha = (pmt.nacha ?? {}) as Record<string, unknown>;
		nachaFields.companyName = typeof nacha.company_name === 'string' ? nacha.company_name : '';
		nachaFields.companyId = typeof nacha.company_id === 'string' ? nacha.company_id : '';
		nachaFields.odfiRouting = typeof nacha.odfi_routing === 'string' ? nacha.odfi_routing : '';
		nachaFields.bankName = typeof nacha.bank_name === 'string' ? nacha.bank_name : '';
		nachaFields.offsetRouting = typeof nacha.offset_routing === 'string' ? nacha.offset_routing : '';
		nachaFields.offsetAccount = '';
		nachaOffsetAccountMask =
			typeof nacha.offset_account === 'string' ? nacha.offset_account : '';
		rememberNachaOnFile(nacha);
		nachaFields.offsetAccountType =
			typeof nacha.offset_account_type === 'string' ? nacha.offset_account_type : '';
	}
	let testingPayments = $state(false);
	let paymentsTestResult = $state<{ success: boolean; message: string } | null>(null);

	async function testPayments() {
		testingPayments = true;
		paymentsTestResult = null;
		try {
			paymentsTestResult = await api.post<{ success: boolean; message: string }>(
				'/api/organization/test-payments',
				{
					provider: paymentsFields.provider,
					api_key: paymentsFields.apiKey,
					org_id: paymentsFields.orgId,
					originating_account_id: paymentsFields.originatingAccount,
					sandbox: paymentsFields.sandbox,
				}
			);
		} catch (err) {
			paymentsTestResult = {
				success: false,
				message: errorMessage(err, m('org.toast.testFailed')),
			};
		} finally {
			testingPayments = false;
		}
	}

	/** PATCH the org's settings. No toast: a save with a credential PUT after
	 *  it toasts once, when both have settled. */
	async function patchOrg(partial: Record<string, unknown>) {
		const data = await api.patch<OrgResponse>('/api/organization', {
			...(partial.company ? { name: company.name.trim() } : {}),
			settings: partial,
		});
		org = data;
	}

	async function patchSettings(section: string, partial: Record<string, unknown>) {
		await patchOrg(partial);
		toast(m('org.toast.sectionSaved', { section }), 'success');
	}

	async function saveProfile() {
		savingProfile = true;
		try {
			await patchSettings(m('org.section.companySaved'), {
				company: {
					address: company.address,
					phone: company.phone,
					website: company.website,
					tax_id: company.taxId,
					vat_registration_number: company.vatNumber,
					companies_house_number: company.companiesHouseNumber,
					logo_url: org?.settings.company.logo_url ?? '',
				},
			});
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			savingProfile = false;
		}
	}

	async function saveDefaults() {
		// The section has no <form>, so the field's own validity cannot block
		// the save: refuse here with the same words the field shows.
		const code = normalizeCurrencyCode(invoiceDefaults.currency);
		if (code === null) {
			invoiceDefaults.currencyTouched = true;
			toast(
				currencyCodeProblem(invoiceDefaults.currency) === 'required'
					? m('common.currencyCode.required')
					: m('common.currencyCode.invalid', { value: invoiceDefaults.currency.trim() }),
				'error'
			);
			return;
		}
		invoiceDefaults.currency = code;
		savingDefaults = true;
		try {
			await patchSettings(m('org.section.defaultsSaved'), {
				invoice_defaults: {
					currency: invoiceDefaults.currency,
					payment_terms: invoiceDefaults.paymentTerms,
					number_prefix: invoiceDefaults.numberPrefix,
					default_gl_account: invoiceDefaults.defaultGl,
					default_cost_center: invoiceDefaults.defaultCostCenter,
				},
			});
			// `invoice_defaults.currency` is a rung of the reporting-currency
			// chain, and the store is session-cached: re-resolve it, or every
			// label on this page (the CFO threshold's included) keeps naming the
			// answer from before the save until a reload.
			orgCurrency.reset();
			void orgCurrency.ensureLoaded();
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			savingDefaults = false;
		}
	}

	async function saveExtraction() {
		const byok = extractionFields.programType === 'byok';
		// Platform mode hides the provider dropdown: resend the saved one, so a
		// choice the admin can no longer see cannot drop the stored key.
		const provider = byok ? extractionFields.provider : (extractionSavedProvider ?? extractionFields.provider);
		// Refuse a switch that would leave BYOK keyless: the stored key goes
		// with the old provider, so the new one's must be typed in this save.
		const needed = byok ? extractionSecretPath(provider) : null;
		const typed = { api_key: extractionFields.apiKey, aws_secret_access_key: extractionFields.awsSecret };
		if (extractionSwitchDropsKey && needed && typed[needed].trim() === '') {
			extractionFields.keyInvalid = true;
			toast(
				m('org.extraction.switchNeedsKey', { provider: extractionProviderLabel(provider) }),
				'error'
			);
			document
				.getElementById(needed === 'api_key' ? 'extraction-api-key' : 'extraction-aws-secret')
				?.focus();
			return;
		}
		extractionFields.keyInvalid = false;
		savingExtraction = true;
		try {
			// Configuration first: a save that switches provider drops the old
			// provider's sealed key server-side, so the key typed for the new one
			// must be written after it.
			await patchOrg({
				extraction: {
					program_type: extractionFields.programType,
					provider,
					aws_access_key_id: extractionFields.awsKeyId,
					aws_region: extractionFields.awsRegion,
					base_url: extractionFields.ollamaUrl,
					model: extractionFields.ollamaModel,
				},
			});
			extractionSavedProvider = provider;
			// …and the panel re-reads what is stored, since that save may have
			// dropped a key the page still believes is there.
			await loadCredentials();
			await saveSecrets('extraction', typed);
			extractionFields.apiKey = extractionFields.awsSecret = '';
			toast(
				m('org.toast.sectionSaved', { section: m('org.section.extractionSaved') }),
				'success'
			);
			unopenable.extraction = false;
		} catch (err) {
			noteUnopenable(err);
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			savingExtraction = false;
		}
	}

	async function saveCards() {
		savingCards = true;
		try {
			await patchOrg({
				cards: {
					enabled: cardsFields.enabled,
					program_type: cardsFields.programType,
					provider: cardsFields.provider || cardAutoProvider(cardsFields.region),
					region: cardsFields.region,
					client_id: cardsFields.clientId,
					customer_hash_id: cardsFields.customerHashId,
					wallet_hash_id: cardsFields.walletHashId,
					default_expiry_days: cardsFields.expiryDays,
					sandbox: cardsFields.sandbox,
				},
			});
			await saveSecrets('cards', {
				api_key: cardsFields.apiKey,
				client_secret: cardsFields.clientSecret
			});
			cardsFields.apiKey = cardsFields.clientSecret = '';
			toast(m('org.toast.sectionSaved', { section: m('org.section.cardsSaved') }), 'success');
			unopenable.cards = false;
		} catch (err) {
			noteUnopenable(err);
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			savingCards = false;
		}
	}

	async function saveSecurity() {
		savingSecurity = true;
		try {
			await patchSettings(m('org.section.securitySaved'), {
				mfa: { required: mfaRequired },
			});
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			savingSecurity = false;
		}
	}

	async function saveFraud() {
		if (!fraud) return;
		savingFraud = true;
		try {
			const domains = personalEmailDomainsText
				.split(/[\n,]+/)
				.map((d) => d.trim().toLowerCase())
				.filter((d) => d.length > 0);
			const payload: FraudRules = { ...fraud, personal_email_domains: domains };
			// The line-total switch has its own panel and writer; the server
			// carries its stored value across this save (decisions §502).
			await patchSettings(m('org.section.fraudSaved'), {
				fraud_rules: withoutOwnedFraudKeys(payload)
			});
			fraud = payload;
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			savingFraud = false;
		}
	}

	function resetFraudToDefaults() {
		if (!fraudDefaults) return;
		fraud = { ...fraudDefaults };
		personalEmailDomainsText = fraudDefaults.personal_email_domains.join('\n');
	}

	async function savePayments() {
		const nacha = nachaForSave({
			company_name: nachaFields.companyName,
			company_id: nachaFields.companyId,
			odfi_routing: nachaFields.odfiRouting,
			bank_name: nachaFields.bankName,
			offset_routing: nachaFields.offsetRouting,
			offset_account: nachaFields.offsetAccount,
			offset_account_type: nachaFields.offsetAccountType
		});
		if (nacha) {
			// The server 422s the same fields (`services/nacha.originator_problems`);
			// naming them here keeps the admin on the form with the reason.
			const problems = nachaProblems(nacha, {
				onFile: nachaOffsetOnFile
			});
			if (problems.length > 0) {
				toast(
					m('org.payments.nachaInvalid', {
						fields: formatList(problems.map((f) => m(NACHA_FIELD_LABEL_KEYS[f])))
					}),
					'error'
				);
				return;
			}
		}
		// Re-send an absent mode as absent unless the admin chose one.
		const sendMode = paymentsModeStored !== null || paymentsFields.mode !== 'processor';
		savingPayments = true;
		try {
			await patchOrg({
				payments: {
					...paymentsExtras,
					...(sendMode ? { mode: paymentsFields.mode } : {}),
					nacha,
					provider: paymentsFields.provider,
					program_type: paymentsProgramType,
					org_id: paymentsFields.orgId,
					originating_account_id: paymentsFields.originatingAccount,
					sandbox: paymentsFields.sandbox,
					cfo_approval_above: paymentsFields.cfoThreshold,
				},
			});
			if (sendMode) paymentsModeStored = paymentsFields.mode;
			// The typed offset account number is now on file: show it as the
			// server does, masked, and clear the input.
			const savedNacha = (
				(org?.settings as unknown as Record<string, unknown> | undefined)?.payments as
					| Record<string, unknown>
					| undefined
			)?.nacha as Record<string, unknown> | null | undefined;
			nachaOffsetAccountMask =
				typeof savedNacha?.offset_account === 'string' ? savedNacha.offset_account : '';
			rememberNachaOnFile(savedNacha);
			nachaFields.offsetAccount = '';
			await saveSecrets('payments', {
				api_key: paymentsFields.apiKey,
				webhook_secret: paymentsFields.webhookSecret
			});
			paymentsFields.apiKey = paymentsFields.webhookSecret = '';
			toast(m('org.toast.sectionSaved', { section: m('org.section.paymentsSaved') }), 'success');
			unopenable.payments = false;
		} catch (err) {
			noteUnopenable(err);
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			savingPayments = false;
		}
	}

	// ── White-label branding ────────────────────────────────────────────
	import { brand } from '#lib/stores/brand.svelte.ts';

	let branding = $state<BrandingFields>({
		productName: '',
		logoUrl: '',
		accentColor: '',
		accentStrongColor: '',
		supportUrl: '',
		legalUrl: '',
		tenantUrlTemplate: '',
		ssoCallbackBaseUrl: ''
	});
	let savingBranding = $state(false);

	// The PLATFORM default (`FEOH_TENANT_URL_TEMPLATE`), read from the public
	// config route so the panel can show an admin what "leave this blank"
	// actually resolves to rather than describing it in the abstract. The org
	// override is what we save; this is only ever displayed.
	let platformTenantUrlTemplate = $state('');

	async function loadPlatformTenantUrl() {
		try {
			const cfg = await api.get<{ tenant_url_template?: string }>('/api/public-config');
			platformTenantUrlTemplate = cfg.tenant_url_template ?? '';
		} catch {
			// Non-fatal: the field still saves, we just can't show the default.
			platformTenantUrlTemplate = '';
		}
	}

	const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

	async function saveBranding() {
		// Client-side validation mirroring the backend BrandConfig guards, so a
		// typo surfaces inline instead of as a 422.
		if (branding.accentColor.trim() && !HEX_RE.test(branding.accentColor.trim())) {
			toast(m('org.branding.toast.accentInvalid'), 'error');
			return;
		}
		if (branding.accentStrongColor.trim() && !HEX_RE.test(branding.accentStrongColor.trim())) {
			toast(m('org.branding.toast.accentStrongInvalid'), 'error');
			return;
		}
		for (const [label, val] of [
			[m('org.branding.label.logoUrl'), branding.logoUrl],
			[m('org.branding.label.supportUrl'), branding.supportUrl],
			[m('org.branding.label.legalUrl'), branding.legalUrl],
			[m('org.branding.label.tenantUrl'), branding.tenantUrlTemplate],
			[m('org.branding.label.ssoCallback'), branding.ssoCallbackBaseUrl],
		] as const) {
			if (val.trim() && !/^https?:\/\//i.test(val.trim())) {
				toast(m('org.branding.toast.urlInvalid', { label }), 'error');
				return;
			}
		}
		savingBranding = true;
		try {
			await api.put('/api/organization/branding', {
				product_name: branding.productName.trim(),
				logo_url: branding.logoUrl.trim(),
				accent_color: branding.accentColor.trim(),
				accent_strong_color: branding.accentStrongColor.trim(),
				support_url: branding.supportUrl.trim(),
				legal_url: branding.legalUrl.trim(),
				tenant_url_template: branding.tenantUrlTemplate.trim(),
				// Sent explicitly on every save. The backend carries an OMITTED
				// value forward precisely so a client that doesn't know about this
				// field can't wipe an IdP-registered callback; since this panel
				// does know about it, sending the current value keeps "clear the
				// box and save" working as the documented rollback.
				sso_callback_base_url: branding.ssoCallbackBaseUrl.trim(),
			});
			// Refresh the live brand store so the sidebar logo/name + theme update
			// without a reload.
			brand.reset();
			await brand.ensureLoadedAndApply();
			toast(m('org.branding.toast.saved'), 'success');
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			savingBranding = false;
		}
	}

	// ── Custom domains (white-label vanity hostnames) ───────────────────
	// Manages settings.brand.custom_domains — the list the backend resolver
	// matches an inbound Host against (with the JWT org-claim cross-check still
	// gating access). See docs/white-label.md § Custom domains.
	let customDomains = $state<string[]>([]);
	let newDomain = $state('');
	let loadingDomains = $state(true);
	let domainsError = $state('');
	let savingDomains = $state(false);
	// The host being removed is armed for a confirm-on-second-click.
	let confirmRemoveDomain = $state<string | null>(null);
	// A REFUSED add/remove, rendered as a persistent inline alert rather than a
	// toast that fades. The backend's refusals are specific and actionable — a
	// host already claimed by another tenant (409), or a host under the
	// platform's own domain — and that message is the whole value of the
	// response, so it stays on screen next to the field that caused it.
	let domainSaveError = $state('');

	// Mirror of the backend normalize_custom_domain: bare, lowercase hostname,
	// no scheme / path / port / spaces. Returns null when there's nothing usable.
	function normalizeDomain(raw: string): string | null {
		let h = (raw || '').trim().toLowerCase();
		if (!h) return null;
		if (h.startsWith('[')) return null; // IPv6 literal — never a custom domain
		// Strip a scheme if the user pasted a full URL.
		h = h.replace(/^https?:\/\//, '');
		// Drop a path/query and a :port suffix.
		h = h.split('/')[0].split('?')[0].split(':')[0];
		if (!h || h.includes(' ')) return null;
		// UX guard, intentionally STRICTER than the backend: require a dotted
		// hostname (label.label…), since a real vanity domain always has a TLD.
		// The backend `normalize_custom_domain` is the authority and accepts a
		// bare single-label host too; this only spares the operator an obvious
		// typo client-side (the safe direction — backend still validates).
		if (!/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/.test(h)) {
			return null;
		}
		return h;
	}

	// One answer for every panel's failed READ. A 4xx carries a refusal the
	// server wrote for this reader (already localized when it is coded), so it
	// is worth showing. A 5xx's message is only its status reason — a 500 read
	// "Internal Server Error" — and a network failure's is the browser's
	// ("Failed to fetch"); neither says more than the panel's own translated
	// fallback, and both are English in five locales.
	function panelLoadError(err: unknown, fallback: string): string {
		if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
			return errorMessage(err, fallback);
		}
		return fallback;
	}

	async function loadCustomDomains() {
		loadingDomains = true;
		domainsError = '';
		try {
			const data = await api.get<{ custom_domains: string[] }>(
				'/api/organization/branding/custom-domains'
			);
			customDomains = data.custom_domains ?? [];
		} catch (err) {
			domainsError = panelLoadError(err, m('org.customDomains.toast.loadFailed'));
		} finally {
			loadingDomains = false;
		}
	}

	// PUT replaces the whole list (the backend endpoint is a full replace),
	// re-reads the normalized result, and refreshes local state.
	async function saveCustomDomains(next: string[]) {
		savingDomains = true;
		domainSaveError = '';
		try {
			const data = await api.put<{ custom_domains: string[] }>(
				'/api/organization/branding/custom-domains',
				{ custom_domains: next }
			);
			customDomains = data.custom_domains ?? [];
			return true;
		} catch (err) {
			// `ApiError.message` is already `formatApiDetail`'d, so a 409
			// ("already registered to another organization") or the
			// platform-domain refusal reads as itself instead of a generic
			// "Failed to save".
			domainSaveError = errorMessage(err, m('org.customDomains.toast.saveFailed'));
			return false;
		} finally {
			savingDomains = false;
		}
	}

	async function addCustomDomain() {
		const host = normalizeDomain(newDomain);
		if (!host) {
			domainSaveError = m('org.customDomains.toast.invalid');
			return;
		}
		if (customDomains.includes(host)) {
			domainSaveError = m('org.customDomains.toast.duplicate');
			return;
		}
		const ok = await saveCustomDomains([...customDomains, host]);
		if (ok) {
			newDomain = '';
			toast(m('org.customDomains.toast.added'), 'success');
		}
	}

	// ── Data residency (GDPR/CCPA region pin) ───────────────────────────
	// Manages settings.residency.region plus the backend's advisory
	// configured-vs-deployed `alignment` verdict — which is the whole point of
	// showing this here: the pin is a commitment, and an admin should be able to
	// see whether the platform is physically honouring it yet. Nothing on this
	// panel blocks; the region never moves data by itself.
	// See docs/data-residency.md.
	interface ResidencyResponse {
		region: string;
		default_region: string;
		supported_regions: string[];
		placement: Record<string, string>;
		alignment: ResidencyAlignment;
	}

	let residencyRegion = $state(''); // the select's bound value
	let residencySavedRegion = $state(''); // last persisted effective region
	let residencyDefault = $state('');
	let residencyRegions = $state<string[]>([]);
	let residencyPlacement = $state<Record<string, string>>({});
	let residencyAlignment = $state<ResidencyAlignment | null>(null);
	let loadingResidency = $state(true);
	let residencyError = $state('');
	let savingResidency = $state(false);

	function applyResidency(data: ResidencyResponse) {
		residencyRegion = data.region;
		residencySavedRegion = data.region;
		residencyDefault = data.default_region;
		residencyRegions = data.supported_regions ?? [];
		residencyPlacement = data.placement ?? {};
		residencyAlignment = data.alignment ?? null;
	}

	async function loadResidency() {
		loadingResidency = true;
		residencyError = '';
		try {
			applyResidency(await api.get<ResidencyResponse>('/api/organization/data-residency'));
		} catch (err) {
			residencyError = panelLoadError(err, m('org.residency.toast.loadFailed'));
		} finally {
			loadingResidency = false;
		}
	}

	// The PUT answers with the same payload as the GET, alignment included, so
	// the verdict for the region just pinned lands without a second round trip.
	async function saveResidency() {
		savingResidency = true;
		try {
			applyResidency(
				await api.put<ResidencyResponse>('/api/organization/data-residency', {
					region: residencyRegion
				})
			);
			toast(m('org.residency.toast.saved'), 'success');
		} catch (err) {
			toast(errorMessage(err, m('org.residency.toast.saveFailed')), 'error');
			// Snap the control back to what is actually persisted, so the panel
			// never shows a region the tenant is not pinned to.
			residencyRegion = residencySavedRegion;
		} finally {
			savingResidency = false;
		}
	}

	async function removeCustomDomain(host: string) {
		// Two-click arm/confirm so a stray click can't drop a live domain.
		if (confirmRemoveDomain !== host) {
			confirmRemoveDomain = host;
			return;
		}
		confirmRemoveDomain = null;
		const ok = await saveCustomDomains(customDomains.filter((d) => d !== host));
		if (ok) toast(m('org.customDomains.toast.removed'), 'success');
	}

	// ── Chat notifications (Slack / Teams) ──────────────────────────────
	// The incoming-webhook URL is the credential for both real providers, and
	// it is WRITE-ONLY end to end: no endpoint returns it, so there is no
	// `chatWebhookUrl` mirror of the persisted value here — only the status the
	// server reports (configured yes/no + the bare host it posts to) and the
	// draft the admin is currently typing. Don't add one.
	// See backend/docs/notifications.md § Rotating the webhook URL.
	let chat = $state<ChatNotificationStatus | null>(null);
	let chatEnabled = $state(false);
	let chatProvider = $state('mock');
	let chatEvents = $state<Record<string, boolean>>({});
	let loadingChat = $state(true);
	let chatError = $state('');
	let savingChat = $state(false);
	let newChatWebhook = $state('');
	let savingChatWebhook = $state(false);
	let confirmRemoveChatWebhook = $state(false);

	function applyChat(data: ChatNotificationStatus) {
		chat = data;
		chatEnabled = data.enabled;
		chatProvider = data.provider ?? 'mock';
		// A missing per-event key means "on" (the backend's opt-out default), so
		// materialize the full map here rather than letting an unchecked box
		// mean "unset".
		chatEvents = Object.fromEntries(
			(data.supported_events ?? []).map((e) => [e, data.events?.[e] ?? true])
		);
	}

	function chatProviderLabel(token: string): string {
		return CHAT_PROVIDER_LABELS[token] ?? token;
	}

	function chatEventLabel(token: string): string {
		return CHAT_EVENT_LABELS[token] ?? token;
	}

	// Chat on, a real provider selected, no webhook stored → the adapter fails
	// closed and silently posts nothing. Surface that rather than let the panel
	// read as configured.
	const chatWebhookMissing = $derived(
		!!chat && chat.enabled && chat.provider !== 'mock' && !chat.webhook_configured
	);

	$effect(() => {
		// Its own effect, the same shape as the email-intake one below and for the
		// same reason: `GET /api/organization/chat-notifications` is
		// `require_roles(ROLE_ADMIN)`, so asking as a clerk has exactly one
		// possible answer — a 403, which this panel then rendered as a
		// `role="alert"` reading "Your role does not permit this action.". That is
		// the anti-pattern the comment above says this design avoids, reached by
		// the one read that was not gated on the role it requires.
		//
		// The role branch still has to run for a non-admin who opens the panel:
		// `loadingChat` starts true, and without clearing it the panel would sit
		// on its loading hint instead of reaching the admin-only one below it.
		if (section !== 'chat' || !userLoaded) return;
		if (!auth.isAdmin) {
			loadingChat = false;
			return;
		}
		once('chat', loadChat);
	});

	async function loadChat() {
		loadingChat = true;
		chatError = '';
		try {
			applyChat(await getChatNotifications());
		} catch (err) {
			chatError = panelLoadError(err, m('org.chat.toast.loadFailed'));
		} finally {
			loadingChat = false;
		}
	}

	async function saveChat() {
		savingChat = true;
		try {
			// The response is authoritative — in particular it re-reports
			// `webhook_configured`, which this save deliberately does not touch.
			applyChat(
				await updateChatNotifications({
					enabled: chatEnabled,
					provider: chatProvider,
					events: chatEvents
				})
			);
			toast(m('org.chat.toast.saved'), 'success');
		} catch (err) {
			toast(errorMessage(err, m('org.chat.toast.saveFailed')), 'error');
		} finally {
			savingChat = false;
		}
	}

	async function saveChatWebhook() {
		const url = newChatWebhook.trim();
		if (!url) {
			toast(m('org.chat.webhook.toast.empty'), 'error');
			return;
		}
		savingChatWebhook = true;
		try {
			applyChat(await rotateChatWebhook(url));
			// Drop the credential from component state the moment it is stored —
			// it is never re-fetchable, so keeping it around buys nothing.
			newChatWebhook = '';
			toast(m('org.chat.webhook.toast.saved'), 'success');
		} catch (err) {
			toast(errorMessage(err, m('org.chat.webhook.toast.saveFailed')), 'error');
		} finally {
			savingChatWebhook = false;
		}
	}

	async function removeChatWebhook() {
		// Two-click arm/confirm — revoking silently stops every approval post.
		if (!confirmRemoveChatWebhook) {
			confirmRemoveChatWebhook = true;
			return;
		}
		confirmRemoveChatWebhook = false;
		savingChatWebhook = true;
		try {
			applyChat(await revokeChatWebhook());
			toast(m('org.chat.webhook.toast.removed'), 'success');
		} catch (err) {
			toast(
				errorMessage(err, m('org.chat.webhook.toast.removeFailed')),
				'error'
			);
		} finally {
			savingChatWebhook = false;
		}
	}

	// ── Single sign-on (OIDC / SAML) ─────────────────────────────────────
	// `settings.sso` has one writer, `PUT /api/organization/sso` (the generic
	// PATCH refuses the key), and its OIDC client secret is write-only: no read
	// returns it, so there is no `ssoClientSecret` mirror of the stored value
	// here — only the field the admin types a NEW secret into, sent when
	// non-blank. Leaving it blank keeps the stored one; removing it is the
	// explicit checkbox. The PUT replaces every other key, so the form round-
	// trips everything the GET served, including the certificate list it has
	// no field for.
	let sso = $state<SsoSettingsStatus | null>(null);
	let loadingSso = $state(true);
	let ssoLoadError = $state('');
	let ssoSaveError = $state('');
	let savingSso = $state(false);
	let ssoFields = $state<SsoFields>({
		enabled: false,
		only: false,
		protocol: 'oidc',
		provider: 'oidc',
		domains: '',
		discoveryUrl: '',
		clientId: '',
		newClientSecret: '',
		clearClientSecret: false,
		idpEntityId: '',
		idpSsoUrl: '',
		idpCert: '',
		idpSloUrl: '',
		spEntityId: ''
	});

	function applySso(data: SsoSettingsStatus) {
		sso = data;
		ssoFields.enabled = data.enabled;
		ssoFields.only = data.sso_only;
		ssoFields.protocol = data.protocol;
		ssoFields.provider = data.provider ?? data.protocol;
		// A list of machine identifiers re-split on save, so a literal comma
		// separator — not `formatList` (see `utils/list.ts`).
		ssoFields.domains = data.allowed_email_domains.join(', ');
		ssoFields.discoveryUrl = data.discovery_url ?? '';
		ssoFields.clientId = data.client_id ?? '';
		ssoFields.newClientSecret = '';
		ssoFields.clearClientSecret = false;
		ssoFields.idpEntityId = data.idp_entity_id ?? '';
		ssoFields.idpSsoUrl = data.idp_sso_url ?? '';
		ssoFields.idpCert = data.idp_x509_cert ?? '';
		ssoFields.idpSloUrl = data.idp_slo_url ?? '';
		ssoFields.spEntityId = data.sp_entity_id ?? '';
	}

	$effect(() => {
		// Same role gate as the chat panel: the GET is admin-only, so a clerk is
		// never sent to collect a 403.
		if (section !== 'sso' || !userLoaded) return;
		if (!auth.isAdmin) {
			loadingSso = false;
			return;
		}
		once('sso', loadSso);
	});

	async function loadSso() {
		loadingSso = true;
		ssoLoadError = '';
		try {
			applySso(await getSsoSettings());
		} catch (err) {
			ssoLoadError = panelLoadError(err, m('orgSso.toast.loadFailed'));
		} finally {
			loadingSso = false;
		}
	}

	function nullable(value: string): string | null {
		const trimmed = value.trim();
		return trimmed ? trimmed : null;
	}

	async function saveSso() {
		if (!sso) return;
		savingSso = true;
		ssoSaveError = '';
		try {
			const secret = ssoFields.newClientSecret.trim();
			applySso(
				await updateSsoSettings({
					enabled: ssoFields.enabled,
					sso_only: ssoFields.only,
					protocol: ssoFields.protocol,
					provider: nullable(ssoFields.provider),
					allowed_email_domains: ssoFields.domains
						.split(/[\s,]+/)
						.map((d) => d.trim())
						.filter(Boolean),
					discovery_url: nullable(ssoFields.discoveryUrl),
					client_id: nullable(ssoFields.clientId),
					...(secret ? { client_secret: secret } : {}),
					...(ssoFields.clearClientSecret && !secret ? { clear_client_secret: true } : {}),
					idp_entity_id: nullable(ssoFields.idpEntityId),
					idp_sso_url: nullable(ssoFields.idpSsoUrl),
					idp_x509_cert: nullable(ssoFields.idpCert),
					idp_x509_cert_multi: sso.idp_x509_cert_multi,
					sp_entity_id: nullable(ssoFields.spEntityId),
					idp_slo_url: nullable(ssoFields.idpSloUrl)
				})
			);
			toast(m('orgSso.toast.saved'), 'success');
		} catch (err) {
			// Stated beside Save (role="alert"), and only there: one error, one
			// signal (`docs/ui-patterns.md` § Toast).
			ssoSaveError =
				err instanceof ApiError
					? (ssoRefusalText(err, m) ?? err.message)
					: m('orgSso.toast.saveFailed');
		} finally {
			savingSso = false;
		}
	}

	// ── Email intake (per-tenant invoices+<token>@ address) ─────────────
	// The address is what vendors mail invoices to, and its `+<token>` part is
	// a BEARER SECRET — anyone holding it can drop a payable into this tenant's
	// queue, and the inbound webhook answers every outcome with the same opaque
	// ack precisely so the token can't be ground out from outside. That makes
	// rotation the only containment for a leak, which is why it ships here
	// rather than living as an endpoint with no caller.
	//
	// `GET` is admin-only, so a non-admin is never asked for it: the panel says
	// so instead of rendering a load failure the reader can't act on.
	let intakeAddress = $state<string | null>(null);
	let intakeEnabled = $state(false);
	// Operator config, not tenant data: false means NO org on this deployment
	// can have an intake address, whatever its token says.
	let intakeDomainConfigured = $state(true);
	let loadingIntake = $state(true);
	let intakeError = $state('');
	let rotatingIntake = $state(false);
	// Armed two-click on Rotate — the same arm/confirm the domain and chat
	// panels use, because the confirm IS the whole safety here: rotating is
	// irreversible and silently breaks every vendor still mailing the old
	// address.
	let confirmRotateIntake = $state(false);
	// The freshly-minted address, shown once through the shared SecretReveal so
	// the admin gets a copy affordance at the one moment it matters. Unlike an
	// API key this value IS re-readable from this page — the reveal's copy says
	// what actually happened (the old address is dead) rather than claiming the
	// value is unrecoverable.
	let rotatedIntakeAddress = $state<string | null>(null);
	let intakeCopied = $state(false);

	$effect(() => {
		// Deliberately its own effect rather than a line in the page's main
		// loader: this one depends on the auth store, and folding it in would
		// make every other panel re-fetch the moment `/me` resolves.
		// The non-admin branch clears `loadingIntake` for the same reason the
		// chat one does — otherwise the panel never leaves its loading hint.
		if (section !== 'email-intake' || !userLoaded) return;
		if (!auth.isAdmin) {
			loadingIntake = false;
			return;
		}
		once('email-intake', loadEmailIntake);
	});

	async function loadEmailIntake() {
		loadingIntake = true;
		intakeError = '';
		try {
			const data = await getEmailIntake();
			intakeAddress = data.address;
			intakeEnabled = data.enabled;
			// `?? true` is the documented degradation: an older backend that does
			// not send the field must leave the panel assuming the deployment is
			// available, not silently reporting a working one as switched off.
			intakeDomainConfigured = data.domain_configured ?? true;
		} catch (err) {
			intakeError = panelLoadError(err, m('org.emailIntake.toast.loadFailed'));
		} finally {
			loadingIntake = false;
		}
	}

	/**
	 * Whether this deployment can serve an intake address at all.
	 *
	 * `GET` returns `address: null` for two different reasons — the platform has
	 * no intake domain (`FEOH_EMAIL_INTAKE_DOMAIN` unset, the committed default,
	 * so the whole channel is off) or this org simply has no token yet — and
	 * they call for opposite copy. `domain_configured` answers it on the first
	 * read; before that field existed the panel could only find out by minting a
	 * throwaway token, which is a write to answer a question.
	 *
	 * `enabled && address === null` is kept as a second, weaker signal: it stays
	 * true against an older backend that doesn't send the field (the client
	 * defaults it to `true`, i.e. assume-available), so the panel degrades to
	 * the previous behaviour rather than mis-reporting a working deployment.
	 */
	const intakeUnavailable = $derived(
		!intakeDomainConfigured || (intakeEnabled && intakeAddress === null)
	);
	const intakeUnprovisioned = $derived(
		intakeDomainConfigured && !intakeEnabled && intakeAddress === null
	);

	async function copyIntakeAddress() {
		if (!intakeAddress) return;
		try {
			await navigator.clipboard.writeText(intakeAddress);
			intakeCopied = true;
			toast(m('org.emailIntake.toast.copied'), 'success');
		} catch {
			// Clipboard access can be denied (permissions, insecure context).
			// The address stays selectable in the DOM, so say so rather than
			// failing silently.
			toast(m('org.emailIntake.toast.copyFailed'), 'error');
		}
	}

	/** First-time provisioning. Invalidates nothing — there is nothing to invalidate. */
	async function generateIntakeAddress() {
		await mintIntakeToken(m('org.emailIntake.toast.created'), false);
	}

	/** Rotation. Armed two-click: the first click only arms + explains. */
	async function rotateIntakeAddress() {
		if (!confirmRotateIntake) {
			confirmRotateIntake = true;
			return;
		}
		confirmRotateIntake = false;
		await mintIntakeToken(m('org.emailIntake.toast.rotated'), true);
	}

	async function mintIntakeToken(successMessage: string, reveal: boolean) {
		rotatingIntake = true;
		intakeError = '';
		try {
			const data = await rotateEmailIntakeToken();
			intakeAddress = data.address;
			// The token is now provisioned and enabled whatever the address came
			// back as. Recording that is what turns a null address from "unknown"
			// into the proven "this deployment has no intake domain" state.
			intakeEnabled = true;
			intakeCopied = false;
			if (data.address === null) {
				// The write succeeded; there is still no address to hand out.
				// Fall through to the unavailable panel rather than toasting a
				// success that produced nothing usable.
				return;
			}
			if (reveal) rotatedIntakeAddress = data.address;
			toast(successMessage, 'success');
		} catch (err) {
			toast(
				errorMessage(err, m('org.emailIntake.toast.rotateFailed')),
				'error'
			);
		} finally {
			rotatingIntake = false;
		}
	}
</script>

<svelte:window
	onclick={(e) => {
		// Un-arm a pending domain-remove confirm when clicking elsewhere.
		if (confirmRemoveDomain && !(e.target as HTMLElement)?.closest?.('.domain-remove')) {
			confirmRemoveDomain = null;
		}
		// Same for the chat-webhook revoke.
		if (
			confirmRemoveChatWebhook &&
			!(e.target as HTMLElement)?.closest?.('.chat-webhook-remove')
		) {
			confirmRemoveChatWebhook = false;
		}
		// Same for the email-intake rotate — an armed rotate left behind is a
		// loaded gun, and this one breaks every vendor's mail path.
		if (confirmRotateIntake && !(e.target as HTMLElement)?.closest?.('.intake-rotate')) {
			confirmRotateIntake = false;
		}
	}}
/>

<PageHeader title={m('org.title')}>
	{#if org}
		{#if readOnly}
			<p class="readonly-banner" data-testid="org-readonly-banner">
				{m('org.readOnly.banner')}
			</p>
		{/if}
		<div class="settings-layout">
			<SettingsRail groups={railGroups} active={section} label={m('org.rail.label')} />

			{#snippet customDomainsIntro()}
				<h2>{m('org.section.customDomains')}</h2>
				<p class="card-hint">
					{m('org.customDomains.hint', { example: 'ap.acmecorp.com', slug: org?.slug ?? 'tenant' })}
				</p>
				<p class="card-hint">
					{m('org.customDomains.setupHint', {
						runbook: 'docs/founder-runbooks/custom-domain-provisioning.md'
					})}
				</p>
			{/snippet}
			{#snippet residencyIntro()}
				<div class="help-row">
					<h2>{m('org.section.dataResidency')}</h2>
					<HelpTip term="data-residency" />
				</div>
				<p class="card-hint">{m('org.residency.hint')}</p>
			{/snippet}

			<!-- A failed panel READ renders here, OUTSIDE the lock below. Retry
			     only re-reads, and a disabled <fieldset> disables every
			     descendant control with no per-control opt-out, so inside it a
			     non-admin was shown a Retry they could not press. Only the
			     panels a non-admin actually loads are listed: chat, email
			     intake, SSO and the close panel make no read for a non-admin
			     (they show their admin-only note instead), so their LoadError
			     can never meet the lock. A new panel a non-admin loads joins
			     this chain, not the fieldset. -->
			{#if section === 'custom-domains' && !loadingDomains && domainsError}
				<div class="sections">
					<section class="card">
						{@render customDomainsIntro()}
						<LoadError message={domainsError} onretry={loadCustomDomains} testId="custom-domains-load-error" />
					</section>
				</div>
			{:else if section === 'residency' && !loadingResidency && residencyError}
				<div class="sections">
					<section class="card">
						{@render residencyIntro()}
						<LoadError message={residencyError} onretry={loadResidency} testId="residency-load-error" />
					</section>
				</div>
			{:else}
			<!-- A single disabled <fieldset> is the whole read-only mode: the
			     attribute natively disables every descendant control, so a panel
			     added later is covered without anyone remembering to gate it.
			     Doing this per-input across ~10 panels is exactly how a gap gets
			     left behind. The server still enforces admin on every write. -->
			<fieldset class="sections" disabled={readOnly}>
				{#if section === 'getting-started'}
					<GettingStartedPanel steps={gettingStartedSteps} />
				{/if}

				{#if section === 'company'}
					<CompanyProfilePanel bind:fields={company} saving={savingProfile} onsave={saveProfile} />
				{/if}

				{#if section === 'defaults'}
					<InvoiceDefaultsPanel bind:fields={invoiceDefaults} saving={savingDefaults} onsave={saveDefaults} />
				{/if}

				{#if section === 'branding'}
					<BrandingPanel
						bind:fields={branding}
						slug={org?.slug ?? ''}
						{platformTenantUrlTemplate}
						saving={savingBranding}
						onsave={saveBranding}
					/>
				{/if}

				{#if section === 'custom-domains'}
					<section class="card">
						{@render customDomainsIntro()}

						{#if loadingDomains}
							<LoadingState label={m('org.customDomains.loading')} />
						{:else}
							{#if domainSaveError}
								<p class="domain-error" role="alert" data-testid="custom-domain-error">
									{domainSaveError}
								</p>
							{/if}
							{#if customDomains.length === 0}
								<p class="card-hint domain-empty">{m('org.customDomains.empty')}</p>
							{:else}
								<ul class="domain-list">
									{#each customDomains as host (host)}
										<li class="domain-row">
											<span class="domain-name mono">{host}</span>
											<span class="domain-remove">
												<button
													type="button"
													class="btn-remove-domain"
													class:armed={confirmRemoveDomain === host}
													disabled={savingDomains}
													aria-label={m('org.customDomains.removeAria', { host })}
													onclick={() => removeCustomDomain(host)}
												>
													{confirmRemoveDomain === host ? m('org.customDomains.confirmRemove') : m('org.customDomains.remove')}
												</button>
											</span>
										</li>
									{/each}
								</ul>
							{/if}

							<form
								class="domain-add"
								onsubmit={(e) => {
									e.preventDefault();
									addCustomDomain();
								}}
							>
								<input
									type="text"
									bind:value={newDomain}
									placeholder={m('org.customDomains.newPlaceholder')}
									aria-label={m('org.customDomains.newAria')}
									autocomplete="off"
									spellcheck="false"
								/>
								<button type="submit" class="btn-save-section" disabled={savingDomains}>
									{savingDomains ? m('org.customDomains.adding') : m('org.customDomains.add')}
								</button>
							</form>
						{/if}
					</section>
				{/if}

				{#if section === 'chat'}
					<section class="card">
						<h2>{m('org.section.chat')}</h2>
						<p class="card-hint">{m('org.chat.hint')}</p>

						{#if !userLoaded || loadingChat}
							<LoadingState label={m('org.chat.loading')} />
						{:else if readOnly}
							<!-- GET is admin-only, so nothing is fetched for a non-admin and
							     there is nothing to show. Say that, exactly as the Email
							     Intake panel does, instead of rendering a 403 the reader
							     cannot act on. -->
							<p class="card-hint" data-testid="chat-admin-only">
								{m('org.chat.adminOnly')}
							</p>
						{:else if chatError}
							<LoadError message={chatError} onretry={loadChat} testId="chat-load-error" />
						{:else if chat}
							<div class="form-grid">
								<label class="switch-row">
									<input type="checkbox" bind:checked={chatEnabled} />
									<span>{m('org.chat.enabled')}</span>
								</label>
								<label>
									{m('org.chat.provider')}
									<select bind:value={chatProvider}>
										{#each chat.supported_providers as p (p)}
											<option value={p}>{chatProviderLabel(p)}</option>
										{/each}
									</select>
								</label>
							</div>

							<fieldset class="chat-events">
								<legend>{m('org.chat.events')}</legend>
								<p class="card-hint">{m('org.chat.eventsHint')}</p>
								{#each chat.supported_events as ev (ev)}
									<label class="switch-row">
										<input
											type="checkbox"
											checked={chatEvents[ev] ?? true}
											onchange={(e) =>
												(chatEvents = {
													...chatEvents,
													[ev]: (e.currentTarget as HTMLInputElement).checked
												})}
										/>
										<span>{chatEventLabel(ev)}</span>
									</label>
								{/each}
							</fieldset>

							<div class="section-footer">
								<button class="btn-save-section" disabled={savingChat} onclick={saveChat}>
									{savingChat ? m('org.common.saving') : m('org.chat.save')}
								</button>
							</div>

							<h3 class="chat-subhead">{m('org.chat.webhook.title')}</h3>
							<p class="card-hint">{m('org.chat.webhook.hint')}</p>

							{#if chatWebhookMissing}
								<p class="chat-warning" role="alert">
									{m('org.chat.webhook.missingWarning', {
										provider: chatProviderLabel(chat.provider ?? '')
									})}
								</p>
							{/if}

							<div class="chat-webhook-status">
								{#if chat.webhook_configured}
									<span class="chat-webhook-set">
										{chat.webhook_host
											? m('org.chat.webhook.configured', { host: chat.webhook_host })
											: m('org.chat.webhook.configuredUnknownHost')}
									</span>
									<span class="chat-webhook-remove">
										<button
											type="button"
											class="btn-remove-domain"
											class:armed={confirmRemoveChatWebhook}
											disabled={savingChatWebhook}
											aria-label={savingChatWebhook
												? undefined
												: confirmRemoveChatWebhook
													? m('org.chat.webhook.confirmRemoveAria')
													: m('org.chat.webhook.removeAria')}
											onclick={removeChatWebhook}
										>
											{confirmRemoveChatWebhook
												? m('org.chat.webhook.confirmRemove')
												: m('org.chat.webhook.remove')}
										</button>
									</span>
								{:else}
									<span class="card-hint">{m('org.chat.webhook.notConfigured')}</span>
								{/if}
							</div>

							<form
								class="domain-add"
								onsubmit={(e) => {
									e.preventDefault();
									saveChatWebhook();
								}}
							>
								<input
									type="text"
									bind:value={newChatWebhook}
									placeholder={m('org.chat.webhook.placeholder')}
									aria-label={m('org.chat.webhook.inputAria')}
									autocomplete="off"
									spellcheck="false"
								/>
								<button type="submit" class="btn-save-section" disabled={savingChatWebhook}>
									{savingChatWebhook
										? m('org.chat.webhook.saving')
										: chat.webhook_configured
											? m('org.chat.webhook.replace')
											: m('org.chat.webhook.set')}
								</button>
							</form>
							<p class="card-hint">{m('org.chat.webhook.rotateHint')}</p>
						{/if}
					</section>
				{/if}

				{#if section === 'email-intake'}
					<section class="card" id="org-email-intake">
						<div class="help-row">
							<h2>{m('org.section.emailIntake')}</h2>
							<HelpTip term="email-intake" />
						</div>
						<p class="card-hint">{m('org.emailIntake.hint')}</p>

						{#if !userLoaded || loadingIntake}
							<LoadingState label={m('org.emailIntake.loading')} />
						{:else if readOnly}
							<!-- GET is admin-only. Say that, rather than firing a request
							     that 403s and rendering a load failure nobody can act on. -->
							<p class="card-hint" data-testid="email-intake-admin-only">
								{m('org.emailIntake.adminOnly')}
							</p>
						{:else if intakeError}
							<LoadError message={intakeError} onretry={loadEmailIntake} testId="email-intake-error" />
						{:else if intakeUnavailable}
							<!-- Proven: a token exists and the server still renders no
							     address, which only happens when the platform has no
							     intake domain. No rotate control here — it would mint
							     another token that still addresses nothing. -->
							<p class="intake-unavailable" data-testid="email-intake-unavailable">
								{m('org.emailIntake.unavailable')}
							</p>
						{:else if intakeUnprovisioned}
							<p class="card-hint" data-testid="email-intake-unprovisioned">
								{m('org.emailIntake.notProvisioned')}
							</p>
							<button
								type="button"
								class="btn-save-section"
								disabled={rotatingIntake}
								onclick={generateIntakeAddress}
							>
								{rotatingIntake
									? m('org.emailIntake.generating')
									: m('org.emailIntake.generate')}
							</button>
						{:else}
							<p class="intake-address-label">{m('org.emailIntake.addressLabel')}</p>
							<div class="intake-address-row">
								<code class="intake-address mono" data-testid="email-intake-address"
									>{intakeAddress}</code
								>
								<button
									type="button"
									class="btn-save-section"
									aria-label={intakeCopied ? undefined : m('org.emailIntake.copyAria')}
									onclick={copyIntakeAddress}
								>
									{intakeCopied ? m('org.emailIntake.copied') : m('org.emailIntake.copy')}
								</button>
							</div>
							<p class="card-hint">{m('org.emailIntake.secretHint')}</p>

							{#if confirmRotateIntake}
								<p class="intake-rotate-warning" role="alert" data-testid="email-intake-rotate-warning">
									{m('org.emailIntake.rotateWarning')}
								</p>
							{/if}
							<span class="intake-rotate">
								<button
									type="button"
									class="btn-remove-domain"
									class:armed={confirmRotateIntake}
									disabled={rotatingIntake}
									aria-label={rotatingIntake
										? undefined
										: confirmRotateIntake
											? m('org.emailIntake.rotateConfirmAria')
											: m('org.emailIntake.rotateAria')}
									onclick={rotateIntakeAddress}
								>
									{rotatingIntake
										? m('org.emailIntake.rotating')
										: confirmRotateIntake
											? m('org.emailIntake.rotateConfirm')
											: m('org.emailIntake.rotate')}
								</button>
							</span>
						{/if}
					</section>
				{/if}

				{#if section === 'residency'}
					<ResidencyPanel
						bind:region={residencyRegion}
						intro={residencyIntro}
						loading={loadingResidency}
						savedRegion={residencySavedRegion}
						defaultRegion={residencyDefault}
						regions={residencyRegions}
						placement={residencyPlacement}
						alignment={residencyAlignment}
						saving={savingResidency}
						onsave={saveResidency}
					/>
				{/if}

				{#if section === 'extraction'}
					<!-- The six panels from here to Fraud Detection have NO non-admin data
					     to show. `services/org_settings_view.py::NON_ADMIN_SETTINGS` is an
					     allow-list and admits none of `extraction`, `cards`, `mfa`,
					     `fraud_rules`, the ERP credentials or the payments credentials —
					     deliberately: most of them ARE third-party credentials (an ERP client
					     secret, a processor credential set, a card API key) and the rest have
					     no non-admin consumer. So for a non-admin those blocks arrive ABSENT
					     and every field falls back to its initializer, which is a platform
					     default wearing the tenant's clothes: Extraction reads "Claude Vision
					     / Platform" whatever the tenant bought, Payments reads "Mock",
					     Security reads MFA-not-required, and Fraud Detection disappeared
					     entirely. A disabled <fieldset> around a wrong value is still a wrong
					     value.

					     Each therefore replaces its BODY with the same admin-only hint the
					     Email Intake panel already uses, keeping its heading and its
					     description. Hiding the sections outright was rejected: the heading
					     is true (the setting exists, and knowing who to ask is the useful
					     part), Getting Started links to the Payments panel, and the page would
					     otherwise carry two vocabularies for one fact — a hint here, silence
					     there. Widening the projection to fill the fields was rejected
					     outright: that is the credential leak `org_settings_view` exists to
					     close.

					     ONE exception inside these six, recorded so nobody reads the hint as
					     a stronger claim than it is: the allow-list does admit
					     `erp.integration_method`, so the ERP panel's routing-mode select
					     alone could honestly be shown. It is not, because the panel is the
					     unit and the field beside it (ERP system) plus every credential below
					     are absent — one live select among fourteen missing ones would be a
					     THIRD treatment for the same fact, and this page has just finished
					     getting down to one. That key's declared non-admin consumer is the
					     workflow builder's ERP hint, which still reads it.
					     decisions §153. -->
					<ExtractionPanel
						bind:fields={extractionFields}
						bind:secretClear
						{readOnly}
						keyStored={extractionKeyStored}
						switchDropsKey={extractionSwitchDropsKey}
						unopenable={unopenable.extraction ?? false}
						oncleared={(status) => onCredentialsCleared('extraction', status)}
						saving={savingExtraction}
						onsave={saveExtraction}
						testing={testingExtraction}
						testResult={extractionTestResult}
						ontest={testExtraction}
					/>
				{/if}

				{#if section === 'erp'}
					<ErpConnectionPanel
						stored={org?.settings.erp}
						{readOnly}
						{userLoaded}
						entitled={erpEntitled}
						mergeEntitled={erpMergeEntitled}
						onsaved={(data) => {
							if (org) org = { ...org, settings: data.settings as unknown as OrgSettings };
						}}
					/>
				{/if}

				{#if section === 'commerce'}
					<CommerceConnectionPanel {readOnly} {userLoaded} />
				{/if}

				{#if section === 'payments'}
					<PaymentsPanel
						bind:fields={paymentsFields}
						bind:nacha={nachaFields}
						bind:secretClear
						offsetAccountMask={nachaOffsetAccountMask}
						{org}
						{readOnly}
						{isStored}
						unopenable={unopenable.payments ?? false}
						oncleared={(status) => onCredentialsCleared('payments', status)}
						saving={savingPayments}
						onsave={savePayments}
						testing={testingPayments}
						testResult={paymentsTestResult}
						ontest={testPayments}
					/>
				{/if}

				{#if section === 'cards'}
					<CardsPanel
						bind:fields={cardsFields}
						bind:secretClear
						{readOnly}
						{isStored}
						unopenable={unopenable.cards ?? false}
						oncleared={(status) => onCredentialsCleared('cards', status)}
						saving={savingCards}
						onsave={saveCards}
					/>
				{/if}

				{#if section === 'security'}
					<SecurityPanel
						bind:mfaRequired
						enforcementActive={mfaEnforcementActive}
						{readOnly}
						saving={savingSecurity}
						onsave={saveSecurity}
					/>
				{/if}

				{#if section === 'sso'}
					<SsoPanel
						bind:fields={ssoFields}
						status={sso}
						{readOnly}
						{userLoaded}
						loading={loadingSso}
						loadError={ssoLoadError}
						onretry={loadSso}
						saveError={ssoSaveError}
						saving={savingSso}
						onsave={saveSso}
					/>
				{/if}

				{#if section === 'scim'}
					<ScimTokenPanel {readOnly} {userLoaded} />
				{/if}

				{#if section === 'fraud'}
					<FraudRulesPanel
						bind:rules={fraud}
						bind:personalEmailDomainsText
						{readOnly}
						saving={savingFraud}
						onsave={saveFraud}
						onreset={resetFraudToDefaults}
					/>
				{/if}

				{#if section === 'data-sync'}
					<DataSyncPanel />
				{/if}

				{#if section === 'plan'}
					<PlanPanel {org} />
				{/if}

				{#if section === 'match-rules'}
					<MatchRulesPanel {readOnly} {userLoaded} />
				{/if}

				{#if section === 'line-total-check'}
					<LineTotalCheckPanel {readOnly} {userLoaded} />
				{/if}

				{#if section === 'exception-agents'}
					<AgentAutonomyPanel {readOnly} {userLoaded} />
				{/if}

				{#if section === 'close'}
					<CloseWorkspacePanel {readOnly} {userLoaded} />
				{/if}
			</fieldset>
			{/if}
		</div>
	{:else if orgLoadFailed}
		<LoadError message={m('org.error.loadFailed')} onretry={loadOrg} testId="org-load-error" />
	{:else}
		<LoadingState label={m('org.loading')} />
	{/if}
</PageHeader>

<!-- The freshly-rotated address, through the same one-time reveal an API-key
     mint and a webhook secret rotation use. The value is re-readable from the
     panel above, so the copy says what is actually irreversible — the OLD
     address is dead — instead of the "shown once" warning those two carry. -->
<SecretReveal
	open={rotatedIntakeAddress !== null}
	ariaLabel={m('org.emailIntake.rotated.aria')}
	heading={m('org.emailIntake.rotated.heading')}
	warningStrong={m('org.emailIntake.rotated.warningStrong')}
	warning={m('org.emailIntake.rotated.warning')}
	secret={rotatedIntakeAddress ?? ''}
	testId="email-intake-address-rotated"
	copyLabel={m('org.emailIntake.copy')}
	copiedLabel={m('org.emailIntake.copied')}
	copiedToast={m('org.emailIntake.toast.copied')}
	copyFailedToast={m('org.emailIntake.toast.copyFailed')}
	doneLabel={m('org.emailIntake.rotated.done')}
	onclose={() => (rotatedIntakeAddress = null)}
/>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
	/* A <fieldset> for the read-only mode's native `disabled` cascade — the UA
	   border/padding/margin have to be reset so it lays out exactly as the
	   <div> it replaced. `min-width: 0` stops the fieldset's default
	   min-content sizing forcing the page wider than its container. */
	/* Rail beside panel. One column on a phone, where the rail collapses to its
	   own disclosure (`ui/SettingsRail.svelte`) and sits above the panel.
	   `minmax(0, 1fr)` rather than `1fr` for the panel track: a grid item's
	   default `min-width: auto` is its CONTENT width, so the widest form in any
	   panel — the ERP credential grid — would push the track past the viewport
	   and scroll the document sideways (WCAG 1.4.10), which is the same trap
	   `.sections`' own `min-width: 0` is there for. */
	.settings-layout {
		display: grid;
		/* Explicit, and `minmax(0, …)` in the ONE-column case too, not just in
		   the two-column rule below. An implicit grid column is auto-sized and a
		   grid item's default `min-width: auto` is its CONTENT width, so the
		   widest panel pushed the document 43px past a 320px viewport — a real
		   WCAG 1.4.10 failure, caught by `tests-e2e/a11y/reflow.spec.ts`.
		   `/organization` only escaped it because its `.sections` fieldset
		   already carried `min-width: 0` for an unrelated reason, which is
		   exactly the kind of accident not to rely on. */
		grid-template-columns: minmax(0, 1fr);
		gap: 20px;
	}

	@media (min-width: 60rem) {
		.settings-layout {
			grid-template-columns: 200px minmax(0, 1fr);
			gap: 28px;
			/* Not `stretch`: the rail is sticky, and a stretched grid item is as
			   tall as the row, which leaves it nothing to stick within. */
			align-items: start;
		}

	}

	.sections {
		display: flex;
		flex-direction: column;
		gap: 16px;
		border: 0;
		padding: 0;
		margin: 0;
		min-width: 0;
	}

	/* Non-admins get the page read-only rather than a form whose every Save
	   403s. Informational, not an error — the muted tint, not danger. */
	.readonly-banner {
		margin: 0 0 16px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.85rem;
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	.intake-address-label {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin: 0 0 6px;
	}

	.intake-address-row {
		display: flex;
		align-items: stretch;
		gap: 8px;
		margin-bottom: 12px;
	}

	.intake-address {
		flex: 1;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 12px;
		font-size: 0.9rem;
		word-break: break-all;
		/* One click selects the whole address — it is copied by hand whenever
		   the clipboard API is unavailable. */
		user-select: all;
	}

	/* The armed rotate's explanation. Tinted-badge recipe: the -tint
	   background with its matching -on-tint text, never the base token. */
	.intake-rotate-warning {
		margin: 0 0 10px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	/* "This deployment has no intake domain" — a standing fact about the
	   install, not something the reader did wrong. */
	.intake-unavailable {
		margin: 0;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 20px 24px;
	}

	.card h2 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0 0 4px;
	}

	.card-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
		margin-top: 14px;
	}

	.full-width {
		grid-column: 1 / -1;
	}

	/* Per-field explanation sitting OUTSIDE the <label> (it is referenced by
	   aria-describedby instead), so it describes the control without being
	   swallowed into its accessible name. */
	.field-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 6px 0 0;
		line-height: 1.5;
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	/* The field label only (the first span of a <label>). */
	label > span:first-child {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	/* Custom domains */
	.domain-list {
		list-style: none;
		margin: 4px 0 16px;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.domain-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 8px 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
	}

	.domain-name {
		font-size: 0.9rem;
		word-break: break-all;
	}

	.btn-remove-domain {
		flex-shrink: 0;
		padding: 4px 12px;
		font-size: 0.82rem;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		cursor: pointer;
	}

	.btn-remove-domain:hover:not(:disabled),
	.btn-remove-domain.armed {
		/* --danger-strong, not --danger: this armed state is a fill carrying
		   white text, and the old #e5484d fallback was 3.91:1. */
		border-color: var(--danger-strong);
		color: #fff;
		background: var(--danger-strong);
	}

	.btn-remove-domain:disabled {
		opacity: 0.6;
		cursor: default;
	}

	.domain-add {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.domain-add input {
		flex: 1;
	}

	/* An in-panel save refusal (a domain add, an SSO save). A panel's failed
	   LOAD is `ui/LoadError` instead, which carries the Retry this line had no
	   room for. */
	.domain-error {
		color: var(--danger);
		font-size: 0.88rem;
		margin: 4px 0 12px;
	}

	/* "Enabled for a real provider but no webhook stored" — the adapter fails
	   closed and posts nothing, so this is a live misconfiguration, not an
	   error the user just caused. Tinted-badge recipe: the -tint background
	   with its matching -on-tint text, never the base token. */
	.chat-warning {
		margin: 4px 0 12px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.chat-subhead {
		margin: 20px 0 4px;
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text);
	}

	.chat-events {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 12px 14px;
		margin: 14px 0 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.chat-events legend {
		padding: 0 6px;
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.chat-events .card-hint {
		margin: 0 0 4px;
	}

	.chat-webhook-status {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin: 8px 0 12px;
		flex-wrap: wrap;
	}

	.chat-webhook-set {
		font-size: 0.88rem;
		color: var(--text);
	}

	.domain-empty {
		margin-bottom: 12px;
	}

	/* The TEXT-entry recipe, carved away from the three controls it also
	   reached but was never written for. Svelte scopes this to
	   `.card.svelte-x input:where(.svelte-x)`, which outranks the global
	   control base in `app.css`.

	   checkbox/radio: `background:` is a SHORTHAND — it reset
	   `background-image`, the drawn tick, so the twelve `.switch-row` toggles
	   rendered identically checked and unchecked.

	   color: adding those two `:not()`s took this selector from 0-0-1 to
	   0-2-1, which TIES `.color-field input[type='color']` (also 0-2-1) — and
	   on a tie the later rule wins, so `width: 100%` started beating the
	   swatch's own `width: 40px`. The branding swatch grew to the full 478px
	   column and squeezed its sibling hex field to 22px, under the WCAG 2.2 AA
	   SC 2.5.8 24px floor (caught by `tests-e2e/a11y/axe.spec.ts`). A native
	   colour swatch is not a text entry: it wants neither this padding nor
	   this width, so it is carved out rather than out-specified. */
	input:not([type='checkbox']):not([type='radio']):not([type='color']),
	select,
	textarea {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
		width: 100%;
		box-sizing: border-box;
	}

	textarea {
		resize: vertical;
	}

	/* Same carve-out: `outline: none` here would strip the checkbox/radio
	   focus ring `app.css` draws for them (WCAG 2.4.7). */
	input:not([type='checkbox']):not([type='radio']):focus,
	select:focus,
	textarea:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}

	.section-footer {
		display: flex;
		/* Same reason as `.erp-test-row` (now in ExtractionPanel): this row
		   carries a Save button, an optional Test button and an optional result message, all of them
		   `white-space: nowrap`. Without a wrap the row cannot shrink below its
		   own max-content width and pushes the whole document sideways at 320px
		   — WCAG 1.4.10. The `gap` replaces the spacing the buttons used to get
		   for free from sitting on one line. */
		flex-wrap: wrap;
		justify-content: flex-start;
		gap: 12px;
		margin-top: 16px;
		padding-top: 14px;
		border-top: 1px solid var(--border);
	}

	.btn-save-section {
		padding: 8px 18px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-save-section:hover:not(:disabled) {
		opacity: 0.85;
	}

	.btn-save-section:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	label.switch-row {
		flex-direction: row;
		align-items: center;
		gap: 10px;
		font-size: 0.9rem;
		color: var(--text);
		cursor: pointer;
	}

	label.switch-row span {
		font-size: 0.9rem;
		font-weight: 400;
		color: var(--text);
		text-transform: none;
		letter-spacing: normal;
	}

	label.switch-row input[type='checkbox'] {
		width: 16px;
		height: 16px;
		accent-color: var(--accent);
		cursor: pointer;
		flex-shrink: 0;
	}

	@media (max-width: 600px) {
		.form-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
