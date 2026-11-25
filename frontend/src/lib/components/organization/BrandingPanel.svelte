<script lang="ts">
	/**
	 * Organization → Branding (white-label).
	 *
	 * The fields are the page's `branding` object, bound in, so an unsaved
	 * edit survives a panel switch (`panelState.ts`). The page owns the save
	 * (with its hex / URL validation) and the platform tenant-URL read; this
	 * panel derives only what it displays from them.
	 */
	import FieldWarning from '#lib/components/ui/FieldWarning.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { accentStrongContrast, accentTextContrast, THEME_NAMES } from '#lib/stores/brandTheme.ts';
	import { formatRatio, WCAG_AA_NORMAL } from '#lib/a11y/contrast.ts';
	import type { BrandingFields } from './panelState.ts';

	let {
		fields = $bindable(),
		slug,
		platformTenantUrlTemplate,
		saving,
		onsave
	}: {
		fields: BrandingFields;
		/** This tenant's slug, substituted into a `{slug}` template. */
		slug: string;
		/**
		 * The PLATFORM default (`FEOH_TENANT_URL_TEMPLATE`), so the panel can
		 * show what "leave this blank" actually resolves to. Display only.
		 */
		platformTenantUrlTemplate: string;
		saving: boolean;
		onsave: () => void;
	} = $props();

	/** Resolve a `{slug}` template against this tenant. */
	function resolveTenantUrl(template: string): string {
		const t = (template || '').trim();
		if (!t) return '';
		return t.replace(/\{slug\}/g, slug);
	}

	const defaultTenantUrl = $derived(resolveTenantUrl(platformTenantUrlTemplate));
	const effectiveTenantUrl = $derived(
		resolveTenantUrl(fields.tenantUrlTemplate) || defaultTenantUrl
	);

	// Default accent tokens (mirror src/app.css :root) so the color pickers show
	// the live default when the org hasn't set one — without writing it back.
	const DEFAULT_ACCENT = '#638cff';
	const DEFAULT_ACCENT_STRONG = '#3f5fd6';

	// The strong accent is written straight into the `--accent-strong` custom
	// property, whose one contract is that white text sits on it. Warn while
	// the field is still being edited; the backend accepts any valid hex and
	// the brand is the tenant's call, so this advises rather than blocks.
	const accentStrongRatio = $derived(accentStrongContrast(fields.accentStrongColor));
	const accentStrongFails = $derived(
		accentStrongRatio !== null && accentStrongRatio < WCAG_AA_NORMAL
	);
	// `accent_color` is also small text (links, the active tab), and no colour
	// reads at 4.5:1 on both palettes' surfaces. Each theme shows the least
	// shade of it that does (`themeAccent`, decisions §478); say so per theme,
	// with the shade, so the change to the brand is never silent.
	const accentText = $derived(accentTextContrast(fields.accentColor));
</script>

<section class="card org-panel" id="org-branding">
	<h2>{m('org.section.branding')}</h2>
	<p class="card-hint">
		{m('org.branding.hint')}
	</p>
	<div class="form-grid">
		<label>
			<span>{m('org.branding.productName')}</span>
			<input
				type="text"
				bind:value={fields.productName}
				placeholder={m('org.branding.productNamePlaceholder')}
				maxlength="120"
			/>
		</label>
		<label>
			<span>{m('org.branding.logoUrl')}</span>
			<input
				type="url"
				bind:value={fields.logoUrl}
				placeholder={m('org.branding.logoUrlPlaceholder')}
			/>
		</label>
		<label>
			<span>{m('org.branding.accentColor')}</span>
			<span class="color-field">
				<input
					type="color"
					aria-label={m('org.branding.accentColorPicker')}
					value={fields.accentColor.trim() || DEFAULT_ACCENT}
					oninput={(e) => (fields.accentColor = e.currentTarget.value)}
				/>
				<input
					type="text"
					bind:value={fields.accentColor}
					placeholder={DEFAULT_ACCENT}
				/>
			</span>
			{#each THEME_NAMES as theme (theme)}
				{@const verdict = accentText?.[theme]}
				<FieldWarning
					show={!!verdict && !verdict.meetsAA}
					testId={`accent-text-contrast-${theme}`}
					message={m(
						theme === 'light'
							? 'org.branding.accentTextLight'
							: 'org.branding.accentTextDark',
						{
							ratio: verdict ? formatRatio(verdict.ratio) : '',
							applied: verdict?.applied ?? ''
						}

					)}
				/>
			{/each}
		</label>
		<label>
			<span>{m('org.branding.accentStrong')}</span>
			<span class="color-field">
				<input
					type="color"
					aria-label={m('org.branding.accentStrongPicker')}
					value={fields.accentStrongColor.trim() || DEFAULT_ACCENT_STRONG}
					oninput={(e) => (fields.accentStrongColor = e.currentTarget.value)}
				/>
				<input
					type="text"
					bind:value={fields.accentStrongColor}
					placeholder={DEFAULT_ACCENT_STRONG}
				/>
			</span>
			<FieldWarning
				show={accentStrongFails}
				testId="accent-strong-contrast-warning"
				message={m('common.contrastWarning', {
					ratio: accentStrongRatio === null ? '' : formatRatio(accentStrongRatio)
				})}
			/>
		</label>
		<label>
			<span>{m('org.branding.supportUrl')}</span>
			<input
				type="url"
				bind:value={fields.supportUrl}
				placeholder={m('org.branding.supportUrlPlaceholder')}
			/>
		</label>
		<label>
			<span>{m('org.branding.legalUrl')}</span>
			<input
				type="url"
				bind:value={fields.legalUrl}
				placeholder={m('org.branding.legalUrlPlaceholder')}
			/>
		</label>
		<div class="full-width">
			<label>
				<span>{m('org.branding.tenantUrl')}</span>
				<input
					type="text"
					bind:value={fields.tenantUrlTemplate}
					placeholder={defaultTenantUrl || m('org.branding.tenantUrlPlaceholder')}
					autocomplete="off"
					spellcheck="false"
					aria-describedby="brand-tenant-url-hint"
				/>
			</label>
			<p class="field-hint" id="brand-tenant-url-hint">
				{m('org.branding.tenantUrlHint')}
				<span class="tenant-url-effective" data-testid="tenant-url-effective">
					{#if fields.tenantUrlTemplate.trim()}
						{m('org.branding.tenantUrlEffective', { url: effectiveTenantUrl })}
					{:else if defaultTenantUrl}
						{m('org.branding.tenantUrlDefault', { url: defaultTenantUrl })}
					{:else}
						{m('org.branding.tenantUrlDefaultUnknown')}
					{/if}
				</span>
			</p>
		</div>
		<div class="full-width">
			<label>
				<span>{m('org.branding.ssoCallback')}</span>
				<input
					type="text"
					bind:value={fields.ssoCallbackBaseUrl}
					placeholder={m('org.branding.ssoCallbackPlaceholder')}
					autocomplete="off"
					spellcheck="false"
					aria-describedby="brand-sso-callback-hint"
				/>
			</label>
			<p class="field-hint sso-callback-hint" id="brand-sso-callback-hint">
				<strong>{m('org.branding.ssoCallbackWarning')}</strong>
				{m('org.branding.ssoCallbackHint')}
			</p>
		</div>
	</div>
	<p class="card-hint">
		{m('org.branding.strongHint')}
	</p>
	<div class="section-footer">
		<button class="btn-save-section" disabled={saving} onclick={onsave}>
			{saving ? m('org.common.saving') : m('org.branding.save')}
		</button>
	</div>
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

	.tenant-url-effective {
		display: block;
		color: var(--text);
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	/* The field label only — not the .color-field wrapper span (reset below). */
	label > span:first-child {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	/* Branding color picker: native swatch + hex text input side by side. */
	.color-field {
		display: flex;
		align-items: center;
		gap: 8px;
		text-transform: none;
		letter-spacing: normal;
	}

	.color-field input[type='color'] {
		width: 40px;
		height: 36px;
		padding: 2px;
		flex-shrink: 0;
		cursor: pointer;
	}

	.color-field input[type='text'] {
		flex: 1;
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
