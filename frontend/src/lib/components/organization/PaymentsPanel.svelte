<script lang="ts">
	/**
	 * Organization → Payments (`settings.payments`): execution mode, the
	 * record-only NACHA file, the processor and the CFO sign-off threshold.
	 *
	 * `fields`, `nacha` and the secret "remove" toggles (`secretClear`) are
	 * page state, bound in, so an unsaved edit survives a panel switch
	 * (`panelState.ts`). The page owns the save (with its NACHA validation and
	 * the carry-back of keys this form does not manage), the connection test
	 * and the unopenable-block recovery.
	 */
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import SecretField from '#lib/components/ui/SecretField.svelte';
	import CredentialResetPrompt from '#lib/components/organization/CredentialResetPrompt.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { orgCurrency } from '#lib/stores/orgSettings.svelte.ts';
	import { NACHA_OFFSET_ACCOUNT_TYPES } from '#lib/types/paymentExecution.ts';
	import type { ProviderCredentialStatus } from '#lib/types/providerCredentials.ts';
	import type { NachaFields, PaymentsFields } from './panelState.ts';

	let {
		fields = $bindable(),
		nacha = $bindable(),
		secretClear = $bindable(),
		offsetAccountMask,
		org,
		readOnly,
		isStored,
		unopenable,
		oncleared,
		saving,
		onsave,
		testing,
		testResult,
		ontest
	}: {
		fields: PaymentsFields;
		nacha: NachaFields;
		/** The page's per-secret "remove" toggles, keyed `block.path`. */
		secretClear: Record<string, boolean>;
		/** The stored offset account number as the server masks it (`****1234`), or ''. */
		offsetAccountMask: string;
		/** For the processor webhook URL this tenant registers. */
		org: { slug: string; created_at: string };
		readOnly: boolean;
		/** Whether the server holds a value at `payments.<path>` (names only). */
		isStored: (block: 'payments', path: string) => boolean;
		/** The stored block can no longer be opened (decisions §464). */
		unopenable: boolean;
		oncleared: (status: ProviderCredentialStatus) => void;
		saving: boolean;
		onsave: () => void;
		testing: boolean;
		testResult: { success: boolean; message: string } | null;
		ontest: () => void;
	} = $props();

	// A product name, not copy: it reads the same in every locale.
	const MODERN_TREASURY = 'Modern Treasury';
</script>

<section class="card org-panel" id="org-payments">
	<div class="help-row">
		<h2>{m('org.section.payments')}</h2>
		<HelpTip term="payment-rail" />
	</div>
	<p class="card-hint">
		{m('org.payments.hint')}
	</p>
	{#if readOnly}
		<p class="card-hint" data-testid="payments-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else}
		<div class="form-grid">
			<label>
				<span>{m('org.payments.mode')}</span>
				<select
					bind:value={fields.mode}
					aria-describedby="org-payments-mode-hint"
					data-testid="payments-mode"
				>
					<option value="processor">{m('org.payments.modeProcessor')}</option>
					<option value="record_only">{m('org.payments.modeRecordOnly')}</option>
				</select>
			</label>
		</div>
		<p class="card-hint" id="org-payments-mode-hint">
			{m('org.payments.modeHint')}
			<HelpTip term="record-only" />
		</p>

		{#if fields.mode === 'record_only'}
			<div class="help-row">
				<h3 class="chat-subhead">{m('org.payments.nachaHeading')}</h3>
				<HelpTip term="nacha-file" />
			</div>
			<p class="card-hint" id="org-payments-nacha-hint">{m('org.payments.nachaHint')}</p>
			<div class="form-grid" data-testid="payments-nacha">
				<label>
					<span>{m('org.payments.nachaField.companyName')}</span>
					<input
						type="text"
						bind:value={nacha.companyName}
						maxlength="16"
						autocomplete="off"
						aria-describedby="org-payments-nacha-hint"
					/>
				</label>
				<label>
					<span>{m('org.payments.nachaField.companyId')}</span>
					<input
						type="text"
						bind:value={nacha.companyId}
						maxlength="10"
						autocomplete="off"
						aria-describedby="org-payments-nacha-hint"
					/>
				</label>
				<label>
					<span>{m('org.payments.nachaField.odfiRouting')}</span>
					<input
						type="text"
						inputmode="numeric"
						bind:value={nacha.odfiRouting}
						maxlength="9"
						autocomplete="off"
						aria-describedby="org-payments-nacha-hint"
					/>
				</label>
				<label>
					<span>{m('org.payments.nachaField.bankName')}</span>
					<input
						type="text"
						bind:value={nacha.bankName}
						maxlength="23"
						autocomplete="off"
						aria-describedby="org-payments-nacha-hint"
					/>
				</label>
			</div>
			<p class="card-hint" id="org-payments-nacha-offset-hint">
				{m('org.payments.nachaOffsetHint')}
			</p>
			<div class="form-grid" data-testid="payments-nacha-offset">
				<label>
					<span>{m('org.payments.nachaField.offsetRouting')}</span>
					<input
						type="text"
						inputmode="numeric"
						bind:value={nacha.offsetRouting}
						maxlength="9"
						autocomplete="off"
						aria-describedby="org-payments-nacha-offset-hint"
					/>
				</label>
				<div>
					<label>
						<span>{m('org.payments.nachaField.offsetAccount')}</span>
						<input
							type="text"
							bind:value={nacha.offsetAccount}
							maxlength="20"
							autocomplete="off"
							placeholder={offsetAccountMask || undefined}
							aria-describedby={offsetAccountMask
								? 'org-payments-nacha-offset-hint org-payments-nacha-offset-onfile'
								: 'org-payments-nacha-offset-hint'}
						/>
					</label>
					<!-- The placeholder alone was the only sign an account is on
					     file: it vanishes on focus and is no accessible
					     description. Visible text outside the <label> (so it is
					     not swallowed into the name), tied to the input. -->
					{#if offsetAccountMask}
						<p
							class="field-hint"
							id="org-payments-nacha-offset-onfile"
							data-testid="payments-nacha-offset-onfile"
						>
							{m('org.payments.nachaOffsetOnFile', { mask: offsetAccountMask })}
						</p>
					{/if}
				</div>
				<label>
					<!-- Named by its label text alone: a <select> inside its
					     <label> otherwise takes the chosen option into its name
					     ("Offset account type None"). -->
					<span id="org-payments-nacha-offset-type-label"
						>{m('org.payments.nachaField.offsetAccountType')}</span
					>
					<select
						bind:value={nacha.offsetAccountType}
						aria-labelledby="org-payments-nacha-offset-type-label"
						aria-describedby="org-payments-nacha-offset-hint"
					>
						<option value="">{m('org.payments.nachaOffsetTypeNone')}</option>
						{#each NACHA_OFFSET_ACCOUNT_TYPES as type (type)}
							<option value={type}>
								{type === 'savings'
									? m('org.payments.nachaOffsetType.savings')
									: m('org.payments.nachaOffsetType.checking')}
							</option>
						{/each}
					</select>
				</label>
			</div>
		{/if}

		<div class="form-grid">
			<label>
				<span>{m('org.payments.provider')}</span>
				<select bind:value={fields.provider}>
					<option value="mock">{m('org.payments.providerMock')}</option>
					<option value="modern_treasury">{MODERN_TREASURY}</option>
				</select>
			</label>
		</div>

		{#if fields.provider === 'modern_treasury'}
			<div class="form-grid">
				<label>
					<span>{m('org.payments.orgId')}</span>
					<input type="text" bind:value={fields.orgId} placeholder={'org_...'} />
				</label>
				<SecretField
					id="payments-api-key"
					label={m('org.payments.apiKey')}
					bind:value={fields.apiKey}
					bind:clear={secretClear['payments.api_key']}
					configured={isStored('payments', 'api_key')}
					testId="payments-api-key"
				/>
				<label>
					<span>{m('org.payments.originatingAccount')}</span>
					<input type="text" bind:value={fields.originatingAccount} placeholder={m('org.payments.originatingAccountPlaceholder')} />
				</label>
				<SecretField
					id="payments-webhook-secret"
					label={m('org.payments.webhookSecret')}
					bind:value={fields.webhookSecret}
					bind:clear={secretClear['payments.webhook_secret']}
					configured={isStored('payments', 'webhook_secret')}
					placeholder={m('org.payments.webhookSecretPlaceholder')}
					testId="payments-webhook-secret"
				/>
				<label class="switch-row">
					<input type="checkbox" bind:checked={fields.sandbox} />
					<span>{m('org.payments.sandbox')}</span>
				</label>
			</div>
			<p class="card-hint">
				{m('org.payments.webhookHint')}
				<code>{org.created_at ? `${window.location.origin.replace(window.location.host, org.slug + '.' + window.location.host)}/api/payments/webhook/${org.slug}/modern_treasury` : '...'}</code>
			</p>
		{/if}

		<div class="form-grid">
			<label>
				<span>{m('org.payments.cfoThreshold', { currency: orgCurrency.label })}</span>
				<input
					type="number"
					min="0"
					step="100"
					placeholder={m('org.payments.cfoThresholdPlaceholder')}
					value={fields.cfoThreshold ?? ''}
					oninput={(e) => {
						const v = (e.currentTarget as HTMLInputElement).value;
						fields.cfoThreshold = v ? parseFloat(v) : null;
					}}
				/>
			</label>
		</div>
		<p class="card-hint">
			{m('org.payments.cfoHint')}
			<HelpTip term="cfo-gate" />
			<HelpTip term="reporting-currency" />
		</p>

		{#if unopenable && auth.isAdmin}
			<CredentialResetPrompt
				block="payments"
				{oncleared}
			/>
		{/if}
		<div class="section-footer">
			<button class="btn-save-section" disabled={saving} onclick={onsave}>
				{saving ? m('org.common.saving') : m('org.payments.save')}
			</button>
			<button class="btn-test" disabled={testing || fields.provider === 'mock'} onclick={ontest}>
				{testing ? m('org.common.testing') : m('org.common.testConnection')}
			</button>
			<!-- Always mounted, so the result is announced when it arrives
			     (WCAG 4.1.3): a live region inserted together with its text
			     is not read by every screen reader. -->
			<span
				class="test-result"
				role="status"
				class:success={testResult?.success === true}
				class:failure={testResult?.success === false}
			>
				{testResult?.message ?? ''}
			</span>
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

	/* Same carve-out: `outline: none` here would strip the checkbox/radio
	   focus ring `app.css` draws for them (WCAG 2.4.7). */
	input:not([type='checkbox']):not([type='radio']):focus,
	select:focus,
	textarea:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}

	.chat-subhead {
		margin: 20px 0 4px;
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text);
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

	.btn-test {
		padding: 8px 18px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-test:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-test:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.test-result {
		font-size: 0.85rem;
		font-weight: 500;
	}

	.test-result.success {
		color: var(--success);
	}

	.test-result.failure {
		color: var(--danger);
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
</style>
