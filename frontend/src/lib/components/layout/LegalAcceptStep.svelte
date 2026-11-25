<script lang="ts">
	/**
	 * The blocking re-prompt: what the root layout draws in place of the app
	 * shell while the signed-in account's `/me` says `legal_acceptance_required`
	 * (`docs/decisions.md` §495, §497). It asks for one thing — acceptance of the
	 * Terms of Service and Privacy Policy versions in force — and offers Log out
	 * as the only other way on.
	 *
	 * A full-page step, not a modal over the shell, for the reasons in §497:
	 * nothing behind it mounts (no page fires its reads before the user has
	 * accepted), there is no dialog to dismiss or escape, and the URL is left
	 * alone, so once `onaccept` resolves the layout renders the page the user
	 * was going to, at the address they asked for. It is the same shape as
	 * `layout/AccountLoadState`.
	 *
	 * The control is the shared `auth/LegalAcceptCheckbox`: a submit with the
	 * box unticked marks it and moves focus to it; a 422
	 * `legal_acceptance_required` on a TICKED box means the versions changed
	 * since this bundle loaded, so the box switches to its "documents changed —
	 * reload" state; anything else (the 429 the endpoint's per-account limit
	 * answers with, a 5xx, the network) is stated in the alert above the
	 * button, through `apiErrorMessage`, which keeps the coded, localized
	 * `rate_limited` sentence and drops anything uncoded to `failed`.
	 *
	 * `onaccept` is `auth.acceptLegal`, `onlogout` is `auth.logout`, passed in so
	 * this stays presentational.
	 */
	import { onMount, tick } from 'svelte';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import LegalAcceptCheckbox from '#lib/components/auth/LegalAcceptCheckbox.svelte';
	import { ApiError } from '#lib/api.ts';
	import { apiErrorMessage } from '#lib/utils/apiError.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	interface Props {
		onaccept: () => Promise<void>;
		onlogout: () => void;
	}

	let { onaccept, onlogout }: Props = $props();

	let accepted = $state(false);
	let legalError = $state('');
	let versionsChanged = $state(false);
	let error = $state('');
	let submitting = $state(false);
	let loggingOut = $state(false);
	let heading: HTMLHeadingElement | undefined = $state();
	let box: LegalAcceptCheckbox | undefined = $state();

	// The step replaces whatever the user was looking at (often first paint),
	// so focus lands on its heading: a screen reader announces what is being
	// asked, and Tab reaches the box next (WCAG 2.4.3).
	onMount(() => {
		heading?.focus();
	});

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (submitting) return;
		error = '';
		if (!accepted) {
			legalError = m('auth.legal.acceptRequired');
			box?.focus();
			return;
		}
		submitting = true;
		try {
			// On success the layout swaps this step for the page — nothing to do.
			await onaccept();
		} catch (err) {
			if (
				err instanceof ApiError &&
				err.status === 422 &&
				err.code === 'legal_acceptance_required'
			) {
				// The box was ticked and still refused: this tab can only send the
				// versions built into it, so re-ticking is a dead end. Say the
				// documents changed and offer the reload.
				versionsChanged = true;
				await tick();
				box?.focus();
			} else {
				error = apiErrorMessage(err, m('shell.legalAccept.failed'));
			}
		} finally {
			submitting = false;
		}
	}

	function logout() {
		if (loggingOut) return;
		loggingOut = true;
		onlogout();
	}
</script>

<div class="legal-step" data-testid="legal-accept-step">
	<form class="panel" onsubmit={submit} novalidate>
		<BrandMark size={36} />
		<h1 bind:this={heading} tabindex="-1">{m('shell.legalAccept.heading')}</h1>
		<p class="body">{m('shell.legalAccept.body')}</p>
		<LegalAcceptCheckbox
			bind:this={box}
			id="legal-reprompt-accept"
			bind:checked={accepted}
			error={legalError}
			{versionsChanged}
			onchange={() => (legalError = '')}
		/>
		{#if error}
			<p class="error" role="alert">{error}</p>
		{/if}
		<div class="actions">
			<button type="submit" class="btn-primary" aria-disabled={submitting}>
				{submitting ? m('shell.legalAccept.continuing') : m('shell.legalAccept.continue')}
			</button>
			<button type="button" class="btn-cancel" onclick={logout} aria-disabled={loggingOut}>
				{m('shell.logOut')}
			</button>
		</div>
	</form>
</div>

<style>
	.legal-step {
		min-height: 100vh;
		display: grid;
		place-items: center;
		padding: 24px 16px;
		background: var(--bg);
	}
	.panel {
		width: min(480px, 100%);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 32px 28px;
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 14px;
	}
	h1 {
		margin: 0;
		font-size: 1.25rem;
		line-height: 1.3;
		color: var(--text);
	}
	.body {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}
	.error {
		margin: 0;
		font-size: 0.85rem;
		color: var(--danger);
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		margin-top: 4px;
	}
</style>
