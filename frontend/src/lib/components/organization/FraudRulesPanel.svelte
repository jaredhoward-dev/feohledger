<script lang="ts">
	/**
	 * Organization → Fraud detection (`settings.fraud_rules`).
	 *
	 * `rules` is the page's composed rule set (the org's overrides over the
	 * admin-only defaults, `null` until both halves have landed) and
	 * `personalEmailDomainsText` the domain list as typed, both bound in, so
	 * an unsaved edit survives a panel switch (`panelState.ts`). The page
	 * owns the compose, the save and the reset to defaults.
	 */
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { FraudRules } from './panelState.ts';

	let {
		rules: fraud = $bindable(),
		personalEmailDomainsText = $bindable(),
		readOnly,
		saving,
		onsave,
		onreset
	}: {
		rules: FraudRules | null;
		personalEmailDomainsText: string;
		readOnly: boolean;
		saving: boolean;
		onsave: () => void;
		onreset: () => void;
	} = $props();
</script>

{#if readOnly || fraud}
	<section class="card org-panel">
		<h2>{m('org.section.fraud')}</h2>
		<p class="card-hint">
			{m('org.fraud.hint')}
			<HelpTip term="exception" />
		</p>
		{#if readOnly}
			<p class="card-hint" data-testid="fraud-admin-only">
				{m('org.readOnly.sectionAdminOnly')}
			</p>
		{:else if fraud}
			<div class="fraud-grid">
				<label class="switch-row">
					<input type="checkbox" bind:checked={fraud.round_amount_enabled} />
					<span>
						<strong>{m('org.fraud.roundAmount')}</strong>
						<span class="rule-hint">
							{m('org.fraud.roundAmountHint', { min: fraud.round_amount_min })}
						</span>
					</span>
				</label>
				<div class="threshold-row">
					<label>
						<span>{m('org.fraud.minAmount')}</span>
						<input
							type="number"
							min="0"
							step="100"
							bind:value={fraud.round_amount_min}
							disabled={!fraud.round_amount_enabled}
						/>
					</label>
				</div>

				<label class="switch-row">
					<input type="checkbox" bind:checked={fraud.future_date_enabled} />
					<span>
						<strong>{m('org.fraud.futureDate')}</strong>
						<span class="rule-hint">
							{m('org.fraud.futureDateHint')}
						</span>
					</span>
				</label>

				<label class="switch-row">
					<input type="checkbox" bind:checked={fraud.rush_payment_enabled} />
					<span>
						<strong>{m('org.fraud.rushPayment')}</strong>
						<span class="rule-hint">
							{m('org.fraud.rushPaymentHint')}
						</span>
					</span>
				</label>
				<div class="threshold-row">
					<label>
						<span>{m('org.fraud.maxDays')}</span>
						<input
							type="number"
							min="0"
							max="30"
							bind:value={fraud.rush_payment_max_days}
							disabled={!fraud.rush_payment_enabled}
						/>
					</label>
				</div>

				<label class="switch-row">
					<input type="checkbox" bind:checked={fraud.new_vendor_large_enabled} />
					<span>
						<strong>{m('org.fraud.newVendorLarge')}</strong>
						<span class="rule-hint">
							{m('org.fraud.newVendorLargeHint', { amount: fraud.new_vendor_large_amount, days: fraud.new_vendor_max_age_days })}
						</span>
					</span>
				</label>
				<div class="threshold-row">
					<label>
						<span>{m('org.fraud.vendorAge')}</span>
						<input
							type="number"
							min="1"
							bind:value={fraud.new_vendor_max_age_days}
							disabled={!fraud.new_vendor_large_enabled}
						/>
					</label>
					<label>
						<span>{m('org.fraud.largeThreshold')}</span>
						<input
							type="number"
							min="0"
							step="500"
							bind:value={fraud.new_vendor_large_amount}
							disabled={!fraud.new_vendor_large_enabled}
						/>
					</label>
				</div>

				<label class="switch-row">
					<input type="checkbox" bind:checked={fraud.bank_change_enabled} />
					<span>
						<strong>{m('org.fraud.bankChange')}</strong>
						<span class="rule-hint">
							{m('org.fraud.bankChangeHint')}
						</span>
					</span>
				</label>

				<label class="switch-row">
					<input type="checkbox" bind:checked={fraud.personal_email_enabled} />
					<span>
						<strong>{m('org.fraud.personalEmail')}</strong>
						<span class="rule-hint">
							{m('org.fraud.personalEmailHint')}
						</span>
					</span>
				</label>
				<div class="threshold-row">
					<label class="full">
						<span>{m('org.fraud.personalEmailDomains')}</span>
						<textarea
							rows="4"
							bind:value={personalEmailDomainsText}
							disabled={!fraud.personal_email_enabled}
						></textarea>
					</label>
				</div>

				<label class="switch-row">
					<input type="checkbox" bind:checked={fraud.stat_anomaly_enabled} />
					<span>
						<strong>{m('org.fraud.statAnomaly')}</strong>
						<span class="rule-hint">
							{m('org.fraud.statAnomalyHint')}
						</span>
					</span>
				</label>
				<div class="threshold-row">
					<label>
						<span>{m('org.fraud.sigma')}</span>
						<input
							type="number"
							min="0.5"
							step="0.1"
							bind:value={fraud.stat_anomaly_sigma}
							disabled={!fraud.stat_anomaly_enabled}
						/>
					</label>
					<label>
						<span>{m('org.fraud.minPriorInvoices')}</span>
						<input
							type="number"
							min="2"
							bind:value={fraud.stat_anomaly_min_history}
							disabled={!fraud.stat_anomaly_enabled}
						/>
					</label>
				</div>

				<label class="switch-row">
					<input type="checkbox" bind:checked={fraud.llm_anomaly_enabled} />
					<span>
						<strong>{m('org.fraud.llmAnomaly')}</strong>
						<span class="rule-hint">
							{m('org.fraud.llmAnomalyHint')}
						</span>
					</span>
				</label>
			</div>

			<div class="section-footer">
				<button
					type="button"
					class="btn-link"
					onclick={onreset}
					disabled={saving}
				>
					{m('org.fraud.resetDefaults')}
				</button>
				<button
					class="btn-save-section"
					disabled={saving}
					onclick={onsave}
				>
					{saving ? m('org.common.saving') : m('org.fraud.save')}
				</button>
			</div>
		{/if}
	</section>
{/if}

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

	/* --- Fraud detection panel --- */

	.fraud-grid {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}

	.fraud-grid label.switch-row {
		align-items: flex-start;
	}

	.fraud-grid .rule-hint {
		display: block;
		margin-top: 2px;
		font-size: 0.8rem;
		color: var(--text-muted);
		font-weight: 400;
	}

	.threshold-row {
		display: grid;
		grid-template-columns: repeat(2, minmax(180px, 1fr));
		gap: 12px;
		margin-left: 26px; /* align under the switch label */
		margin-bottom: 4px;
	}

	.threshold-row label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.threshold-row label.full {
		grid-column: 1 / -1;
	}

	.threshold-row label span {
		font-size: 0.72rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	/* Text-entry recipe. No checkbox sits under this selector today, but it
	   outranks the global control base in `app.css`, so the carve-out keeps a
	   later one from losing its tick (`background:` resets the drawn mark). */
	.threshold-row input:not([type='checkbox']):not([type='radio']),
	.threshold-row textarea {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
	}

	.threshold-row textarea {
		resize: vertical;
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.82rem;
	}

	.threshold-row input:not([type='checkbox']):not([type='radio']):focus,
	.threshold-row textarea:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}

	.threshold-row input:disabled,
	.threshold-row textarea:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-link {
		background: none;
		border: none;
		color: var(--text-muted);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
		padding: 0;
		margin-right: auto;
	}

	.btn-link:hover:not(:disabled) {
		color: var(--accent);
	}

	@media (max-width: 600px) {
		.threshold-row {
			grid-template-columns: 1fr;
		}

	}
</style>
