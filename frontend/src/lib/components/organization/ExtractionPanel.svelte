<script lang="ts">
	/**
	 * Organization → AI extraction (`settings.extraction`).
	 *
	 * The fields are the page's `extractionFields` object and the secret
	 * "remove" toggles its shared `secretClear` map, both bound in, so an
	 * unsaved edit survives a panel switch (`panelState.ts`). The page owns
	 * the save, the connection test, which provider the stored key belongs to
	 * (`keyStored`, `switchDropsKey`, decisions §444) and the unopenable-block
	 * recovery.
	 */
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import SecretField from '#lib/components/ui/SecretField.svelte';
	import CredentialResetPrompt from '#lib/components/organization/CredentialResetPrompt.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import type { ProviderCredentialStatus } from '#lib/types/providerCredentials.ts';
	import {
		EXTRACTION_PROVIDERS,
		extractionProviderLabel,
		type ExtractionFields
	} from './panelState.ts';

	let {
		fields = $bindable(),
		secretClear = $bindable(),
		readOnly,
		keyStored,
		switchDropsKey,
		unopenable,
		oncleared,
		saving,
		onsave,
		testing,
		testResult,
		ontest
	}: {
		fields: ExtractionFields;
		/** The page's per-secret "remove" toggles, keyed `block.path`. */
		secretClear: Record<string, boolean>;
		readOnly: boolean;
		/** A key is stored at `extraction.<path>` for the provider now selected. */
		keyStored: (path: string) => boolean;
		/** A key is stored for the saved provider, and this save would drop it. */
		switchDropsKey: boolean;
		/** The stored block can no longer be opened (decisions §464). */
		unopenable: boolean;
		oncleared: (status: ProviderCredentialStatus) => void;
		saving: boolean;
		onsave: () => void;
		testing: boolean;
		testResult: { success: boolean; message: string } | null;
		ontest: () => void;
	} = $props();

	// Model names, not copy: they read the same in every locale.
	const OLLAMA_MODELS = [
		{ value: 'llama3.2-vision:11b', name: 'Llama 3.2 Vision 11B' },
		{ value: 'llama3.2-vision:90b', name: 'Llama 3.2 Vision 90B' },
		{ value: 'llava:13b', name: 'LLaVA 13B' },
		{ value: 'llava:34b', name: 'LLaVA 34B' }
	];
</script>

<section class="card org-panel">
	<div class="help-row">
		<h2>{m('org.section.extraction')}</h2>
		<HelpTip term="extraction" />
	</div>
	<p class="card-hint">{m('org.extraction.hint')}</p>
	{#if readOnly}
		<p class="card-hint" data-testid="extraction-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else}
		<div class="form-grid">
			<label>
				<span>{m('org.extraction.program')}</span>
				<select bind:value={fields.programType}>
					<option value="platform">{m('org.extraction.programPlatform')}</option>
					<option value="byok">{m('org.extraction.programByok')}</option>
				</select>
			</label>
			{#if fields.programType === 'byok'}
				<label>
					<span>{m('org.extraction.provider')}</span>
					<select bind:value={fields.provider}>
						{#each EXTRACTION_PROVIDERS as p}
							<option value={p.value}>{extractionProviderLabel(p.value)}</option>
						{/each}
					</select>
				</label>
			{:else}
				<label>
					<span>{m('org.extraction.provider')}</span>
					<input type="text" value="Claude Vision (Anthropic)" disabled />
				</label>
			{/if}
		</div>

		{#if fields.programType === 'platform'}
			<p class="card-hint after-control">{m('org.extraction.platformHint')}</p>
		{:else if fields.provider === 'claude_vision' || fields.provider === 'openai_vision'}
			<div class="form-grid">
				<SecretField
					id="extraction-api-key"
					label={fields.provider === 'claude_vision' ? m('org.extraction.anthropicKey') : m('org.extraction.openaiKey')}
					bind:value={fields.apiKey}
					bind:clear={secretClear['extraction.api_key']}
					configured={keyStored('api_key')}
					placeholder={'sk-...'}
					testId="extraction-api-key"
					required={switchDropsKey}
					invalid={fields.keyInvalid}
					oninput={() => (fields.keyInvalid = false)}
					describedBy={switchDropsKey ? 'extraction-switch-hint' : undefined}
				/>
			</div>
			{#if switchDropsKey}
				<p class="card-hint" id="extraction-switch-hint" data-testid="extraction-switch-hint">
					{m('org.extraction.switchDropsKey', { provider: extractionProviderLabel(fields.provider) })}
				</p>
			{/if}
		{:else if fields.provider === 'aws_textract'}
			<div class="form-grid">
				<label>
					<span>{m('org.extraction.awsKeyId')}</span>
					<input
						type="text"
						bind:value={fields.awsKeyId}
						autocomplete="off"
						spellcheck="false"
					/>
				</label>
				<SecretField
					id="extraction-aws-secret"
					label={m('org.extraction.awsSecret')}
					bind:value={fields.awsSecret}
					bind:clear={secretClear['extraction.aws_secret_access_key']}
					configured={keyStored('aws_secret_access_key')}
					testId="extraction-aws-secret"
					required={switchDropsKey}
					invalid={fields.keyInvalid}
					oninput={() => (fields.keyInvalid = false)}
					describedBy={switchDropsKey ? 'extraction-switch-hint' : undefined}
				/>
				<label>
					<span>{m('org.extraction.awsRegion')}</span>
					<input type="text" bind:value={fields.awsRegion} placeholder={'us-east-1'} />
				</label>
			</div>
			{#if switchDropsKey}
				<p class="card-hint" id="extraction-switch-hint" data-testid="extraction-switch-hint">
					{m('org.extraction.switchDropsKey', { provider: extractionProviderLabel(fields.provider) })}
				</p>
			{/if}
		{:else if fields.provider === 'ollama'}
			<div class="form-grid">
				<label>
					<span>{m('org.extraction.ollamaUrl')}</span>
					<input type="url" bind:value={fields.ollamaUrl} placeholder={'http://localhost:11434'} />
				</label>
				<label>
					<span>{m('org.extraction.model')}</span>
					<select bind:value={fields.ollamaModel}>
						{#each OLLAMA_MODELS as model (model.value)}
							<option value={model.value}>{model.name}</option>
						{/each}
					</select>
				</label>
			</div>
			<p class="card-hint after-control">{m('org.extraction.ollamaHint')} <code>{`brew install ollama && ollama pull ${fields.ollamaModel}`}</code></p>
		{/if}

		{#if unopenable && auth.isAdmin}
			<CredentialResetPrompt
				block="extraction"
				{oncleared}
			/>
		{/if}
		<div class="erp-test-row">
			<button class="btn-save-section" disabled={saving} onclick={onsave}>
				{saving ? m('org.common.saving') : m('org.extraction.save')}
			</button>
			<button class="btn-test" disabled={testing} onclick={ontest}>
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

	/* A hint under the provider picker it qualifies, not at the top of a card. */
	.card-hint.after-control {
		margin-top: 10px;
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

	.erp-test-row {
		display: flex;
		align-items: center;
		/* Wraps because `.btn-test` is `white-space: nowrap` (142px) and sits
		   beside a result message: without this the row could not shrink and
		   pushed the whole page into a horizontal scrollbar at 320px — WCAG
		   1.4.10, the same failure the section tab bar had. Caught when the
		   reflow guard started visiting a grouped route. */
		flex-wrap: wrap;
		gap: 12px;
		margin-top: 16px;
		padding-top: 14px;
		border-top: 1px solid var(--border);
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
</style>
