<script lang="ts">
	/**
	 * Organization → Single sign-on (OIDC / SAML).
	 *
	 * `fields` is the page's form state, bound in, so an unsaved edit survives
	 * a panel switch (`panelState.ts`); `status` is the last GET / PUT answer
	 * the page holds. The page owns the admin-gated load, the save (which
	 * round-trips every key the GET served) and its refusal text.
	 */
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import PlanUpgradeNotice from '#lib/components/ui/PlanUpgradeNotice.svelte';
	import { ssoFieldLabels } from '#lib/api/ssoRefusal.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { FEATURE_SSO, FEATURE_SSO_ENFORCEMENT } from '#lib/types/planFeatures.ts';
	import { SSO_PROVIDER_LABELS, type SsoSettingsStatus } from '#lib/types/ssoSettings.ts';
	import { formatList } from '#lib/utils/list.ts';
	import type { SsoFields } from './panelState.ts';

	let {
		fields = $bindable(),
		status,
		readOnly,
		userLoaded,
		loading,
		loadError,
		onretry,
		saveError,
		saving,
		onsave
	}: {
		fields: SsoFields;
		/** The stored settings as last served, or `null` before the first read. */
		status: SsoSettingsStatus | null;
		readOnly: boolean;
		userLoaded: boolean;
		loading: boolean;
		loadError: string;
		onretry: () => void;
		/** The last save's refusal, stated beside Save (role="alert"). */
		saveError: string;
		saving: boolean;
		onsave: () => void;
	} = $props();

	const ssoEntitled = $derived(auth.hasFeature(FEATURE_SSO));
	const ssoEnforcementEntitled = $derived(auth.hasFeature(FEATURE_SSO_ENFORCEMENT));

	const SSO_PROVIDERS = Object.keys(SSO_PROVIDER_LABELS);

	function ssoProviderLabel(token: string): string {
		return token === 'oidc' || token === 'saml'
			? m('orgSso.provider.generic')
			: (SSO_PROVIDER_LABELS[token] ?? token);
	}

	// The server's own list of what the selected protocol still lacks, named by
	// the labels this panel shows. Recomputed from the SAVED state, so it
	// describes what sign-in sees now, not the unsaved form.
	const ssoMissingLabels = $derived(
		status && status.idp_config_missing.length
			? ssoFieldLabels(status.idp_config_missing, m)
			: null
	);
</script>

<section class="card" data-testid="sso-panel">
	<h2>{m('orgSso.title')}</h2>
	<p class="card-hint">{m('orgSso.hint')}</p>

	{#if !userLoaded || loading}
		<LoadingState label={m('orgSso.loading')} />
	{:else if readOnly}
		<p class="card-hint" data-testid="sso-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else if loadError}
		<LoadError message={loadError} {onretry} testId="sso-load-error" />
	{:else if status}
		{#if status.password_sign_in_closed}
			<p class="sso-status" data-testid="sso-status-closed">
				{m('orgSso.status.passwordClosed')}
			</p>
		{:else if status.enabled && status.idp_config_missing.length}
			<p class="chat-warning" role="alert" data-testid="sso-status-incomplete">
				{ssoMissingLabels
					? m('orgSso.status.incomplete', { fields: formatList(ssoMissingLabels) })
					: m('orgSso.status.incompleteUnknown')}
			</p>
		{/if}

		{#if !ssoEntitled}
			<PlanUpgradeNotice feature={FEATURE_SSO} testId="sso-plan-upgrade" />
		{/if}

		<div class="form-grid">
			<label class="switch-row full-width">
				<!-- Off is always allowed; ON needs the plan. -->
				<input
					type="checkbox"
					bind:checked={fields.enabled}
					disabled={!ssoEntitled && !fields.enabled}
					data-testid="sso-enabled-toggle"
				/>
				<span>{m('orgSso.enabled')}</span>
			</label>
			<label>
				<span>{m('orgSso.field.protocol')}</span>
				<select bind:value={fields.protocol}>
					<option value="oidc">{m('orgSso.protocol.oidc')}</option>
					<option value="saml">{m('orgSso.protocol.saml')}</option>
				</select>
			</label>
			<label>
				<span>{m('orgSso.field.provider')}</span>
				<select bind:value={fields.provider}>
					{#each SSO_PROVIDERS as p (p)}
						<option value={p}>{ssoProviderLabel(p)}</option>
					{/each}
				</select>
			</label>

			{#if fields.protocol === 'oidc'}
				<label class="full-width">
					<span>{m('orgSso.field.discoveryUrl')}</span>
					<input
						type="url"
						bind:value={fields.discoveryUrl}
						placeholder={'https://example.okta.com/.well-known/openid-configuration'}
						autocomplete="off"
						spellcheck="false"
					/>
				</label>
				<label>
					<span>{m('orgSso.field.clientId')}</span>
					<input
						type="text"
						bind:value={fields.clientId}
						autocomplete="off"
						spellcheck="false"
					/>
				</label>
				<div>
					<label>
						<span>{m('orgSso.field.clientSecret')}</span>
						<input
							type="password"
							bind:value={fields.newClientSecret}
							placeholder={status.client_secret_configured
								? m('orgSso.secret.keepPlaceholder')
								: ''}
							autocomplete="new-password"
							spellcheck="false"
							aria-describedby="sso-secret-hint"
							data-testid="sso-client-secret"
						/>
					</label>
					<p class="field-hint" id="sso-secret-hint">
						{status.client_secret_configured
							? m('orgSso.secret.configured')
							: m('orgSso.secret.notConfigured')}
					</p>
					{#if status.client_secret_configured}
						<label class="switch-row">
							<input type="checkbox" bind:checked={fields.clearClientSecret} />
							<span>{m('orgSso.secret.clear')}</span>
						</label>
					{/if}
				</div>
			{:else}
				<label class="full-width">
					<span>{m('orgSso.field.idpEntityId')}</span>
					<input
						type="text"
						bind:value={fields.idpEntityId}
						autocomplete="off"
						spellcheck="false"
					/>
				</label>
				<label class="full-width">
					<span>{m('orgSso.field.idpSsoUrl')}</span>
					<input
						type="url"
						bind:value={fields.idpSsoUrl}
						autocomplete="off"
						spellcheck="false"
					/>
				</label>
				<label class="full-width">
					<span>{m('orgSso.field.idpCert')}</span>
					<textarea
						bind:value={fields.idpCert}
						rows="4"
						spellcheck="false"
						placeholder={'-----BEGIN CERTIFICATE-----'}
					></textarea>
				</label>
				<label>
					<span>{m('orgSso.field.idpSloUrl')}</span>
					<input
						type="url"
						bind:value={fields.idpSloUrl}
						autocomplete="off"
						spellcheck="false"
					/>
				</label>
				<label>
					<span>{m('orgSso.field.spEntityId')}</span>
					<input
						type="text"
						bind:value={fields.spEntityId}
						placeholder={status.saml_sp_entity_id}
						autocomplete="off"
						spellcheck="false"
					/>
				</label>
			{/if}

			<div class="full-width">
				<label>
					<span>{m('orgSso.field.allowedDomains')}</span>
					<input
						type="text"
						bind:value={fields.domains}
						placeholder={'example.com, example.co.uk'}
						autocomplete="off"
						spellcheck="false"
						aria-describedby="sso-domains-hint"
					/>
				</label>
				<p class="field-hint" id="sso-domains-hint">{m('orgSso.domainsHint')}</p>
			</div>
		</div>

		<h3 class="chat-subhead">{m('orgSso.register.title')}</h3>
		<p class="card-hint">{m('orgSso.register.hint')}</p>
		<dl class="sso-register">
			{#if fields.protocol === 'oidc'}
				<dt>{m('orgSso.register.redirectUri')}</dt>
				<dd><code>{status.oidc_redirect_uri}</code></dd>
			{:else}
				<dt>{m('orgSso.register.acsUrl')}</dt>
				<dd><code>{status.saml_acs_url}</code></dd>
				<dt>{m('orgSso.register.spEntityId')}</dt>
				<dd><code>{status.saml_sp_entity_id}</code></dd>
			{/if}
		</dl>

		<div class="sso-only-row">
			<label class="switch-row">
				<input
					type="checkbox"
					bind:checked={fields.only}
					disabled={!ssoEnforcementEntitled && !fields.only}
					aria-describedby="sso-only-hint"
					data-testid="sso-only-toggle"
				/>
				<span>{m('orgSso.ssoOnly')}</span>
			</label>
			<p class="field-hint" id="sso-only-hint">{m('orgSso.ssoOnlyHint')}</p>
			{#if ssoEntitled && !ssoEnforcementEntitled}
				<PlanUpgradeNotice
					feature={FEATURE_SSO_ENFORCEMENT}
					testId="sso-only-plan-upgrade"
				/>
			{/if}
		</div>

		{#if saveError}
			<p class="chat-error" role="alert" data-testid="sso-save-error">{saveError}</p>
		{/if}

		<div class="section-footer">
			<button
				class="btn-save-section"
				disabled={saving}
				onclick={onsave}
				data-testid="sso-save"
			>
				{saving ? m('org.common.saving') : m('orgSso.save')}
			</button>
		</div>
	{/if}
</section>

<style>
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

	/* An in-panel save refusal (a domain add, an SSO save). A panel's failed
	   LOAD is `ui/LoadError` instead, which carries the Retry this line had no
	   room for. */
	.chat-error {
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

	/* SSO-only in force: a state, not a problem — the success tint recipe. */
	.sso-status {
		margin: 4px 0 12px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	/* What the admin pastes into the IdP. Long URLs wrap rather than push the
	   card past a 320px viewport (WCAG 1.4.10). */
	.sso-register {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 6px 14px;
		margin: 0 0 16px;
		font-size: 0.85rem;
	}

	.sso-register dt {
		color: var(--text-muted);
	}

	.sso-register dd {
		margin: 0;
		min-width: 0;
	}

	.sso-register code {
		font-family: var(--font-mono);
		overflow-wrap: anywhere;
	}

	.sso-only-row {
		margin: 4px 0 8px;
	}

	.chat-subhead {
		margin: 20px 0 4px;
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text);
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

		.sso-register {
			grid-template-columns: 1fr;
		}

	}
</style>
