<script lang="ts">
	/**
	 * Organization → Chat notifications (Slack / Teams).
	 *
	 * `fields` is the page's form state (and the webhook draft), bound in, so
	 * an unsaved edit survives a panel switch (`panelState.ts`); `status` is
	 * what the server last reported. The page owns the admin-gated load, both
	 * writes and the armed revoke, which a click elsewhere disarms from the
	 * page's window handler on `.chat-webhook-remove`.
	 */
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import {
		CHAT_EVENT_LABELS,
		CHAT_PROVIDER_LABELS,
		type ChatNotificationStatus
	} from '#lib/types/chatNotifications.ts';
	import type { ChatFields } from './panelState.ts';

	let {
		fields = $bindable(),
		status,
		readOnly,
		userLoaded,
		loading,
		loadError,
		onretry,
		saving,
		onsave,
		savingWebhook,
		confirmRemoveWebhook,
		onsetwebhook,
		onremovewebhook
	}: {
		fields: ChatFields;
		/** The server's report (configured yes/no + bare host), or `null` before the read. */
		status: ChatNotificationStatus | null;
		readOnly: boolean;
		userLoaded: boolean;
		loading: boolean;
		loadError: string;
		onretry: () => void;
		saving: boolean;
		onsave: () => void;
		savingWebhook: boolean;
		/** The revoke is armed for a confirm-on-second-click. */
		confirmRemoveWebhook: boolean;
		onsetwebhook: () => void;
		onremovewebhook: () => void;
	} = $props();

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
		!!status && status.enabled && status.provider !== 'mock' && !status.webhook_configured
	);
</script>

<section class="card">
	<h2>{m('org.section.chat')}</h2>
	<p class="card-hint">{m('org.chat.hint')}</p>

	{#if !userLoaded || loading}
		<LoadingState label={m('org.chat.loading')} />
	{:else if readOnly}
		<!-- GET is admin-only, so nothing is fetched for a non-admin and
		     there is nothing to show. Say that, exactly as the Email
		     Intake panel does, instead of rendering a 403 the reader
		     cannot act on. -->
		<p class="card-hint" data-testid="chat-admin-only">
			{m('org.chat.adminOnly')}
		</p>
	{:else if loadError}
		<LoadError message={loadError} {onretry} testId="chat-load-error" />
	{:else if status}
		<div class="form-grid">
			<label class="switch-row">
				<input type="checkbox" bind:checked={fields.enabled} />
				<span>{m('org.chat.enabled')}</span>
			</label>
			<label>
				{m('org.chat.provider')}
				<select bind:value={fields.provider}>
					{#each status.supported_providers as p (p)}
						<option value={p}>{chatProviderLabel(p)}</option>
					{/each}
				</select>
			</label>
		</div>

		<fieldset class="chat-events">
			<legend>{m('org.chat.events')}</legend>
			<p class="card-hint">{m('org.chat.eventsHint')}</p>
			{#each status.supported_events as ev (ev)}
				<label class="switch-row">
					<input
						type="checkbox"
						checked={fields.events[ev] ?? true}
						onchange={(e) =>
							(fields.events = {
								...fields.events,
								[ev]: (e.currentTarget as HTMLInputElement).checked
							})}
					/>
					<span>{chatEventLabel(ev)}</span>
				</label>
			{/each}
		</fieldset>

		<div class="section-footer">
			<button class="btn-save-section" disabled={saving} onclick={onsave}>
				{saving ? m('org.common.saving') : m('org.chat.save')}
			</button>
		</div>

		<h3 class="chat-subhead">{m('org.chat.webhook.title')}</h3>
		<p class="card-hint">{m('org.chat.webhook.hint')}</p>

		{#if chatWebhookMissing}
			<p class="chat-warning" role="alert">
				{m('org.chat.webhook.missingWarning', {
					provider: chatProviderLabel(status.provider ?? '')
				})}
			</p>
		{/if}

		<div class="chat-webhook-status">
			{#if status.webhook_configured}
				<span class="chat-webhook-set">
					{status.webhook_host
						? m('org.chat.webhook.configured', { host: status.webhook_host })
						: m('org.chat.webhook.configuredUnknownHost')}
				</span>
				<span class="chat-webhook-remove">
					<button
						type="button"
						class="btn-remove-domain"
						class:armed={confirmRemoveWebhook}
						disabled={savingWebhook}
						aria-label={savingWebhook
							? undefined
							: confirmRemoveWebhook
								? m('org.chat.webhook.confirmRemoveAria')
								: m('org.chat.webhook.removeAria')}
						onclick={onremovewebhook}
					>
						{confirmRemoveWebhook
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
				onsetwebhook();
			}}
		>
			<input
				type="text"
				bind:value={fields.newWebhook}
				placeholder={m('org.chat.webhook.placeholder')}
				aria-label={m('org.chat.webhook.inputAria')}
				autocomplete="off"
				spellcheck="false"
			/>
			<button type="submit" class="btn-save-section" disabled={savingWebhook}>
				{savingWebhook
					? m('org.chat.webhook.saving')
					: status.webhook_configured
						? m('org.chat.webhook.replace')
						: m('org.chat.webhook.set')}
			</button>
		</form>
		<p class="card-hint">{m('org.chat.webhook.rotateHint')}</p>
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
</style>
