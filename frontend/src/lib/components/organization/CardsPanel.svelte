<script lang="ts">
	/**
	 * Organization → Virtual cards (`settings.cards`).
	 *
	 * The fields are the page's `cardsFields` object and the secret "remove"
	 * toggles its shared `secretClear` map, both bound in, so an unsaved edit
	 * survives a panel switch (`panelState.ts`). The page owns the save, the
	 * stored-credential status and the unopenable-block recovery.
	 */
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import SecretField from '#lib/components/ui/SecretField.svelte';
	import CredentialResetPrompt from '#lib/components/organization/CredentialResetPrompt.svelte';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import type { ProviderCredentialStatus } from '#lib/types/providerCredentials.ts';
	import { CARD_REGIONS, cardAutoProvider, type CardsFields } from './panelState.ts';

	let {
		fields = $bindable(),
		secretClear = $bindable(),
		readOnly,
		isStored,
		unopenable,
		oncleared,
		saving,
		onsave
	}: {
		fields: CardsFields;
		/** The page's per-secret "remove" toggles, keyed `block.path`. */
		secretClear: Record<string, boolean>;
		readOnly: boolean;
		/** Whether the server holds a value at `cards.<path>` (names only). */
		isStored: (block: 'cards', path: string) => boolean;
		/** The stored block can no longer be opened (decisions §464). */
		unopenable: boolean;
		oncleared: (status: ProviderCredentialStatus) => void;
		saving: boolean;
		onsave: () => void;
	} = $props();

	const regionNames = $derived(new Intl.DisplayNames([currentLocale()], { type: 'region' }));
	function cardRegionLabel(r: (typeof CARD_REGIONS)[number]): string {
		const country = regionNames.of(r.iso) ?? r.value;
		return r.eu ? m('org.cards.regionEu', { country }) : country;
	}

	let autoProvider = $derived(cardAutoProvider(fields.region));
	let effectiveProvider = $derived(fields.provider || autoProvider);
</script>

<section class="card">
	<div class="help-row">
		<h2>{m('org.section.cards')}</h2>
		<HelpTip term="virtual-card" />
	</div>
	<p class="card-hint">{m('org.cards.hint')}</p>
	{#if readOnly}
		<p class="card-hint" data-testid="cards-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else}
		<div class="form-grid">
			<label>
				<span>{m('org.cards.enabled')}</span>
				<select bind:value={fields.enabled}>
					<option value={false}>{m('org.cards.disabled')}</option>
					<option value={true}>{m('org.cards.enabledOn')}</option>
				</select>
			</label>
			<label>
				<span>{m('org.cards.program')}</span>
				<select bind:value={fields.programType}>
					<option value="platform">{m('org.cards.programPlatform')}</option>
					<option value="byok">{m('org.cards.programByok')}</option>
				</select>
			</label>
			<label>
				<span>{m('org.cards.region')}</span>
				<select bind:value={fields.region}>
					{#each CARD_REGIONS as r (r.value)}
						<option value={r.value}>{cardRegionLabel(r)}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>{m('org.cards.expiryDays')}</span>
				<input type="number" min="1" max="90" bind:value={fields.expiryDays} />
			</label>
		</div>

		{#if fields.enabled && fields.programType === 'platform'}
			<p class="card-hint after-control">{m('org.cards.platformHint', { provider: autoProvider === 'lithic' ? 'Lithic' : 'Nium' })}</p>
		{/if}

		{#if fields.enabled && fields.programType === 'byok'}
			<div class="form-grid">
				<label>
					<span>{m('org.cards.provider')}</span>
					<select bind:value={fields.provider}>
						<option value="">{m('org.cards.providerAuto', { provider: autoProvider === 'lithic' ? 'Lithic' : 'Nium' })}</option>
						<option value="lithic">{m('org.cards.providerLithic')}</option>
						<option value="nium">{m('org.cards.providerNium')}</option>
					</select>
				</label>
			</div>

			{#if effectiveProvider === 'lithic'}
				<div class="form-grid">
					<SecretField
						id="cards-api-key"
						label={m('org.cards.lithicApiKey')}
						bind:value={fields.apiKey}
						bind:clear={secretClear['cards.api_key']}
						configured={isStored('cards', 'api_key')}
						placeholder={'api-key-...'}
						testId="cards-api-key"
					/>
					<label>
						<span>{m('org.cards.sandboxMode')}</span>
						<select bind:value={fields.sandbox}>
							<option value={true}>{m('org.cards.sandboxTesting')}</option>
							<option value={false}>{m('org.cards.production')}</option>
						</select>
					</label>
				</div>
			{:else if effectiveProvider === 'nium'}
				<div class="form-grid">
					<label>
						<span>{m('org.cards.clientId')}</span>
						<input type="text" bind:value={fields.clientId} />
					</label>
					<SecretField
						id="cards-client-secret"
						label={m('org.cards.clientSecret')}
						bind:value={fields.clientSecret}
						bind:clear={secretClear['cards.client_secret']}
						configured={isStored('cards', 'client_secret')}
						testId="cards-client-secret"
					/>
					<label>
						<span>{m('org.cards.customerHashId')}</span>
						<input type="text" bind:value={fields.customerHashId} />
					</label>
					<label>
						<span>{m('org.cards.walletHashId')}</span>
						<input type="text" bind:value={fields.walletHashId} />
					</label>
					<label>
						<span>{m('org.cards.sandboxMode')}</span>
						<select bind:value={fields.sandbox}>
							<option value={true}>{m('org.cards.sandboxTesting')}</option>
							<option value={false}>{m('org.cards.production')}</option>
						</select>
					</label>
				</div>
			{/if}
		{/if}

		{#if unopenable && auth.isAdmin}
			<CredentialResetPrompt
				block="cards"
				{oncleared}
			/>
		{/if}
		<div class="section-footer">
			<button class="btn-save-section" disabled={saving} onclick={onsave}>
				{saving ? m('org.common.saving') : m('org.cards.save')}
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

	/* A hint under the provider picker it qualifies, not at the top of a card. */
	.card-hint.after-control {
		margin-top: 10px;
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
		margin-top: 14px;
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

	.section-footer {
		display: flex;
		/* Same reason as `.erp-test-row` below: this row carries a Save button, an
		   optional Test button and an optional result message, all of them
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

	@media (max-width: 600px) {
		.form-grid {
			grid-template-columns: 1fr;
		}

	}
</style>
