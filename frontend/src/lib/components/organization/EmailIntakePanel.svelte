<script lang="ts">
	/**
	 * Organization → Email intake (the per-tenant `invoices+<token>@` address).
	 * Its `+<token>` part is a BEARER SECRET, so rotating it is the only
	 * containment for a leak.
	 *
	 * Pure view: no field state. The page owns the admin-gated load, copy,
	 * generate and rotate, the one-time reveal of a rotated address, and the
	 * armed rotate, which a click elsewhere disarms from its window handler on
	 * `.intake-rotate`.
	 */
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		readOnly,
		userLoaded,
		loading,
		loadError,
		onretry,
		unavailable,
		unprovisioned,
		address,
		copied,
		oncopy,
		rotating,
		confirmRotate,
		ongenerate,
		onrotate
	}: {
		readOnly: boolean;
		userLoaded: boolean;
		loading: boolean;
		loadError: string;
		onretry: () => void;
		/** Proven: the platform has no intake domain, so no address can exist. */
		unavailable: boolean;
		/** No token minted yet. */
		unprovisioned: boolean;
		address: string | null;
		copied: boolean;
		oncopy: () => void;
		rotating: boolean;
		/** The rotate is armed for a confirm-on-second-click. */
		confirmRotate: boolean;
		ongenerate: () => void;
		onrotate: () => void;
	} = $props();
</script>

<section class="card" id="org-email-intake">
	<div class="help-row">
		<h2>{m('org.section.emailIntake')}</h2>
		<HelpTip term="email-intake" />
	</div>
	<p class="card-hint">{m('org.emailIntake.hint')}</p>

	{#if !userLoaded || loading}
		<LoadingState label={m('org.emailIntake.loading')} />
	{:else if readOnly}
		<!-- GET is admin-only. Say that, rather than firing a request
		     that 403s and rendering a load failure nobody can act on. -->
		<p class="card-hint" data-testid="email-intake-admin-only">
			{m('org.emailIntake.adminOnly')}
		</p>
	{:else if loadError}
		<LoadError message={loadError} {onretry} testId="email-intake-error" />
	{:else if unavailable}
		<!-- Proven: a token exists and the server still renders no
		     address, which only happens when the platform has no
		     intake domain. No rotate control here — it would mint
		     another token that still addresses nothing. -->
		<p class="intake-unavailable" data-testid="email-intake-unavailable">
			{m('org.emailIntake.unavailable')}
		</p>
	{:else if unprovisioned}
		<p class="card-hint" data-testid="email-intake-unprovisioned">
			{m('org.emailIntake.notProvisioned')}
		</p>
		<button
			type="button"
			class="btn-save-section"
			disabled={rotating}
			onclick={ongenerate}
		>
			{rotating
				? m('org.emailIntake.generating')
				: m('org.emailIntake.generate')}
		</button>
	{:else}
		<p class="intake-address-label">{m('org.emailIntake.addressLabel')}</p>
		<div class="intake-address-row">
			<code class="intake-address mono" data-testid="email-intake-address"
				>{address}</code
			>
			<button
				type="button"
				class="btn-save-section"
				aria-label={copied ? undefined : m('org.emailIntake.copyAria')}
				onclick={oncopy}
			>
				{copied ? m('org.emailIntake.copied') : m('org.emailIntake.copy')}
			</button>
		</div>
		<p class="card-hint">{m('org.emailIntake.secretHint')}</p>

		{#if confirmRotate}
			<p class="intake-rotate-warning" role="alert" data-testid="email-intake-rotate-warning">
				{m('org.emailIntake.rotateWarning')}
			</p>
		{/if}
		<span class="intake-rotate">
			<button
				type="button"
				class="btn-remove-domain"
				class:armed={confirmRotate}
				disabled={rotating}
				aria-label={rotating
					? undefined
					: confirmRotate
						? m('org.emailIntake.rotateConfirmAria')
						: m('org.emailIntake.rotateAria')}
				onclick={onrotate}
			>
				{rotating
					? m('org.emailIntake.rotating')
					: confirmRotate
						? m('org.emailIntake.rotateConfirm')
						: m('org.emailIntake.rotate')}
			</button>
		</span>
	{/if}
</section>

<style>
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
