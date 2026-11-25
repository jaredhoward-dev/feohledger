<script lang="ts">
	/**
	 * Organization → Invoice defaults.
	 *
	 * Pure view: the fields are the page's `invoiceDefaults` state object,
	 * bound in, so an unsaved edit survives a panel switch (`panelState.ts`).
	 * The page owns the save (`saveDefaults`, which validates the currency
	 * code and re-resolves the reporting currency) and renders this inside
	 * its read-only `<fieldset>`.
	 */
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import CurrencyInput from '#lib/components/ui/CurrencyInput.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { InvoiceDefaultsFields } from './panelState.ts';

	let {
		fields = $bindable(),
		saving,
		onsave
	}: {
		fields: InvoiceDefaultsFields;
		saving: boolean;
		onsave: () => void;
	} = $props();

	// The stored value is the English term the backend and the ERP exports
	// read, so it stays the option's `value`; only the label is localized.
	const PAYMENT_TERMS = [
		'Due on Receipt',
		'Net 10',
		'Net 15',
		'Net 30',
		'Net 45',
		'Net 60',
		'Net 90',
		'2/10 Net 30'
	] as const;
	// A term saved through the API (or before this list) that is not one of the
	// presets is still offered, so the select shows what is actually stored
	// rather than a blank box that silently overwrites it on the next save.
	const paymentTermOptions = $derived(
		(PAYMENT_TERMS as readonly string[]).includes(fields.paymentTerms)
			? [...PAYMENT_TERMS]
			: [...PAYMENT_TERMS, fields.paymentTerms]
	);
	function paymentTermLabel(term: string): string {
		if (term === 'Due on Receipt') return m('org.defaults.terms.dueOnReceipt');
		if (term === '2/10 Net 30') return m('org.defaults.terms.twoTenNet30');
		const net = /^Net (\d+)$/.exec(term);
		return net ? m('org.defaults.terms.net', { days: Number(net[1]) }) : term;
	}
</script>

<section class="card" id="org-defaults">
	<h2>{m('org.section.defaults')}</h2>
	<p class="card-hint">{m('org.defaults.hint')} <HelpTip term="reporting-currency" /></p>
	<div class="form-grid">
		<label>
			<span>{m('org.defaults.currency')}</span>
			<!-- Any ISO 4217 code: six hardcoded options left a PLN or
			     ZAR organization unable to set its own default, which is
			     also a rung of the reporting currency (decisions §381). -->
			<CurrencyInput
				bind:value={fields.currency}
				bind:touched={fields.currencyTouched}
				testid="org-default-currency"
			/>
		</label>
		<label>
			<span>{m('org.defaults.paymentTerms')}</span>
			<select bind:value={fields.paymentTerms}>
				{#each paymentTermOptions as term (term)}
					<option value={term}>{paymentTermLabel(term)}</option>
				{/each}
			</select>
		</label>
		<label>
			<span>{m('org.defaults.numberPrefix')}</span>
			<input type="text" bind:value={fields.numberPrefix} placeholder={'INV-'} />
		</label>
		<label>
			<span>{m('org.defaults.defaultGl')}</span>
			<input type="text" bind:value={fields.defaultGl} placeholder={m('org.defaults.defaultGlPlaceholder')} />
		</label>
		<label>
			<span>{m('org.defaults.defaultCostCenter')}</span>
			<input type="text" bind:value={fields.defaultCostCenter} placeholder={m('org.defaults.defaultCostCenterPlaceholder')} />
		</label>
	</div>
	<div class="section-footer">
		<button class="btn-save-section" disabled={saving} onclick={onsave}>
			{saving ? m('org.common.saving') : m('org.defaults.save')}
		</button>
	</div>
</section>

<style>
	/* The page's panel recipe, copied rather than shared: Svelte scopes a
	   page's <style> to the page, so a panel component carries the rules its
	   own markup needs (the pattern every `organization/` panel follows). */
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

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	label > span:first-child {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	/* The page's text-entry recipe; see its comment on the checkbox / radio /
	   colour carve-outs. */
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

	input:not([type='checkbox']):not([type='radio']):focus,
	select:focus,
	textarea:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}

	/* Wraps at 320px (WCAG 1.4.10): see the page's `.section-footer`. */
	.section-footer {
		display: flex;
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

	@media (max-width: 600px) {
		.form-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
