<script lang="ts">
	/**
	 * Organization → Company profile.
	 *
	 * Pure view: the fields are the page's `company` state object, bound in, so
	 * an unsaved edit survives a panel switch (`panelState.ts`). The page owns
	 * the save (`saveProfile`, which also sends the org's `name`) and renders
	 * this inside its read-only `<fieldset>`.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { CompanyFields } from './panelState.ts';

	let {
		fields = $bindable(),
		saving,
		onsave
	}: {
		fields: CompanyFields;
		saving: boolean;
		onsave: () => void;
	} = $props();
</script>

<section class="card org-panel" id="org-company">
	<h2>{m('org.section.company')}</h2>
	<div class="form-grid">
		<label>
			<span>{m('org.company.name')}</span>
			<input type="text" bind:value={fields.name} />
		</label>
		<label>
			<span>{m('org.company.taxId')}</span>
			<input type="text" bind:value={fields.taxId} placeholder={m('org.company.taxIdPlaceholder')} />
		</label>
		<label>
			<span>{m('org.company.vatNumber')}</span>
			<input type="text" bind:value={fields.vatNumber} />
		</label>
		<label>
			<span>{m('org.company.companiesHouseNumber')}</span>
			<input type="text" bind:value={fields.companiesHouseNumber} />
		</label>
		<label class="full-width">
			<span>{m('org.company.address')}</span>
			<textarea bind:value={fields.address} rows="2" placeholder={m('org.company.addressPlaceholder')}></textarea>
		</label>
		<label>
			<span>{m('org.company.phone')}</span>
			<input type="tel" bind:value={fields.phone} />
		</label>
		<label>
			<span>{m('org.company.website')}</span>
			<input type="url" bind:value={fields.website} placeholder={'https://'} />
		</label>
	</div>
	<div class="section-footer">
		<button class="btn-save-section" disabled={saving} onclick={onsave}>
			{saving ? m('org.common.saving') : m('org.company.save')}
		</button>
	</div>
</section>

<style>
	/* The page's panel recipe, copied: Svelte scopes a page's <style> to the
	   page, so a panel carries the rules its own markup needs. The form layout
	   (`.form-grid`, `.full-width`, `.section-footer`) is shared instead, from
	   app.css under `.org-panel` (decisions §530). */
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
</style>
