<script lang="ts">
	import { tick } from 'svelte';
	import HCaptcha from '#lib/components/auth/HCaptcha.svelte';
	import { api } from '#lib/api.ts';
	import { apiErrorMessage } from '#lib/utils/apiError.ts';
	import { currentLocale, m } from '#lib/i18n/store.svelte.ts';

	/**
	 * "Email me my workspace address" (`POST /api/workspace-lookup`, decisions
	 * §518) — the recovery for someone who has forgotten which workspace they
	 * sign in on. The server answers every address the same way and only ever
	 * tells the mailbox itself, by email, so this form says the same thing for
	 * every address too: "if it has an account, we've emailed it".
	 *
	 * Lives on the apex's `/find-workspace`, under `id="email"` so the landing
	 * page and a workspace's sign-in page can link straight to it.
	 */
	interface Props {
		/** `hcaptcha_sitekey` from `/api/public-config`; empty = no widget. */
		sitekey: string;
	}

	let { sitekey }: Props = $props();

	let email = $state('');
	let error = $state('');
	let submitting = $state(false);
	// The address the confirmation names; `null` while the form is showing.
	let sentTo = $state<string | null>(null);
	let captchaToken = $state<string | null>(null);
	let captchaLoadFailed = $state(false);
	let captcha: HCaptcha | undefined = $state();
	let input = $state<HTMLInputElement | null>(null);
	let sentHeading = $state<HTMLHeadingElement | null>(null);

	async function onSubmit(event: SubmitEvent) {
		event.preventDefault();
		if (submitting) return;
		error = '';
		const address = email.trim();
		if (!input?.checkValidity() || !address) {
			error = m('marketing.findWorkspace.lookup.invalid');
			input?.focus();
			return;
		}
		if (sitekey && !captchaToken) {
			error = captchaLoadFailed
				? m('auth.signup.captchaBlockedSubmit')
				: m('auth.signup.captchaRequired');
			return;
		}
		submitting = true;
		try {
			await api.post('/api/workspace-lookup', {
				email: address,
				captcha_token: captchaToken,
				// The email is written in it when the account has no language set.
				locale: currentLocale()
			});
			sentTo = address;
			// The button that had focus left with the form; land on the
			// confirmation's heading so it is announced (WCAG 2.4.3, 4.1.3).
			await tick();
			sentHeading?.focus();
		} catch (err) {
			error = apiErrorMessage(err, m('marketing.findWorkspace.lookup.failed'));
		} finally {
			// Spent either way: the server verifies the captcha first.
			captcha?.reset();
			submitting = false;
		}
	}

	async function again() {
		sentTo = null;
		error = '';
		captchaToken = null;
		await tick();
		input?.focus();
		input?.select();
	}
</script>

<section class="lookup" id="email" aria-labelledby="lookup-heading">
	{#if sentTo === null}
		<form class="lookup-form" onsubmit={onSubmit} novalidate>
			<h2 id="lookup-heading">{m('marketing.findWorkspace.lookup.heading')}</h2>
			<p class="sub">{m('marketing.findWorkspace.lookup.intro')}</p>

			<!-- Inserted with its text, so it is announced on arrival; no standing
			     empty alert region beside the address form's own. -->
			{#if error}
				<div class="error" role="alert">{error}</div>
			{/if}

			<label>
				<span>{m('marketing.findWorkspace.lookup.label')}</span>
				<input
					bind:this={input}
					bind:value={email}
					type="email"
					required
					maxlength="320"
					autocomplete="email"
					autocapitalize="off"
					spellcheck="false"
					oninput={() => (error = '')}
				/>
			</label>

			<HCaptcha
				bind:this={captcha}
				{sitekey}
				bind:token={captchaToken}
				bind:loadFailed={captchaLoadFailed}
				blockedId="lookup-captcha-blocked"
			/>

			<button
				type="submit"
				disabled={submitting}
				aria-describedby={sitekey && captchaLoadFailed ? 'lookup-captcha-blocked' : undefined}
			>
				{submitting
					? m('marketing.findWorkspace.lookup.sending')
					: m('marketing.findWorkspace.lookup.submit')}
			</button>
		</form>
	{:else}
		<div class="sent" role="status">
			<h2 id="lookup-heading" tabindex="-1" bind:this={sentHeading}>
				{m('marketing.findWorkspace.lookup.sentHeading')}
			</h2>
			<p class="sub">{m('marketing.findWorkspace.lookup.sent', { email: sentTo })}</p>
			<button type="button" class="link-btn" onclick={again}>
				{m('marketing.findWorkspace.lookup.again')}
			</button>
		</div>
	{/if}
</section>

<style>
	/* Field chrome, the submit button and the error banner come from AuthShell. */
	.lookup {
		margin-top: 18px;
		padding-top: 16px;
		border-top: 1px solid var(--border);
		scroll-margin-top: 24px;
	}
	.lookup-form,
	.sent {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	h2 {
		margin: 0;
		font-size: 1rem;
		font-weight: 700;
		color: var(--text);
	}
	h2:focus {
		outline: none;
	}
	.sub {
		margin: 0;
		font-size: 0.88rem;
		line-height: 1.55;
		color: var(--text-muted);
		overflow-wrap: anywhere;
	}
	.link-btn {
		align-self: flex-start;
		padding: 0;
		border: none;
		background: none;
		color: var(--accent-on-tint);
		font: inherit;
		font-size: 0.88rem;
		font-weight: 600;
		text-decoration: underline;
		text-underline-offset: 2px;
		cursor: pointer;
	}
</style>
