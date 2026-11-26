<script lang="ts">
	/**
	 * Organization → Join a partner (`docs/decisions.md` §14, §574).
	 *
	 * Mints a single-use, short-lived link code FOR this workspace through
	 * `POST /api/organization/partner-link-code`. Handing the code to a partner
	 * (reseller) is how this workspace consents to being attached as one of
	 * its child tenants; the partner redeems it on their Partner Admin page.
	 *
	 * Consenting to BE adopted is not a partner act, so this lives here and is
	 * open to any admin — the Partner Admin page itself now needs the
	 * operator-granted partner capability. The mint is audited server-side
	 * (`partner.link_code_issued`). The code is shown once, in the shared
	 * `ui/SecretReveal` dialog (focus moves into it, the copy is acknowledged,
	 * focus returns to Generate on close), and kept only while that dialog is
	 * open; it is never stored or put in a URL. Losing it costs a new mint.
	 *
	 * A refused mint (the platform has no signing key: 503
	 * `partner_linking_disabled`) is stated in the reader's language from its
	 * code, in place beside the button; anything uncoded reads the generic
	 * failure, never the server's raw English.
	 *
	 * Admin-only end to end: `readOnly` renders the notice and makes no call.
	 */
	import SecretReveal from '#lib/components/ui/SecretReveal.svelte';
	import { mintLinkCode } from '#lib/api/partner.ts';
	import { ApiError } from '#lib/api.ts';
	import { localizeApiDetail } from '#lib/api/codedRefusals.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		readOnly
	}: {
		/** The caller is not an admin: render the notice, call nothing. */
		readOnly: boolean;
	} = $props();

	let minted = $state<{ code: string; expiresInMinutes: number } | null>(null);
	let minting = $state(false);
	let failure = $state<string | null>(null);

	async function mintCode() {
		// `aria-disabled`, not `disabled`, while minting: a disabled button
		// throws focus to <body>, and the dialog would then hand it back there
		// on close instead of to this button (WCAG 2.4.3).
		if (minting) return;
		minting = true;
		failure = null;
		try {
			const res = await mintLinkCode();
			minted = { code: res.link_code, expiresInMinutes: res.expires_in_minutes };
		} catch (e) {
			failure =
				(e instanceof ApiError ? localizeApiDetail(e.detail, m) : null) ??
				m('org.partnerLink.mintFailed');
		} finally {
			minting = false;
		}
	}
</script>

<section class="card" aria-labelledby="partner-link-title" data-testid="link-code-panel">
	<h2 id="partner-link-title">{m('org.partnerLink.heading')}</h2>
	<p class="card-hint">{m('org.partnerLink.hint')}</p>

	{#if readOnly}
		<p class="card-hint" data-testid="partner-link-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else}
		{#if failure}
			<p class="mint-failure" id="partner-link-failure" role="alert" data-testid="partner-link-failure">
				{failure}
			</p>
		{/if}
		<div class="link-code-actions">
			<button
				type="button"
				class="btn-cancel"
				onclick={mintCode}
				aria-disabled={minting}
				aria-describedby={failure ? 'partner-link-failure' : undefined}
			>
				{minting ? m('org.partnerLink.generating') : m('org.partnerLink.generate')}
			</button>
		</div>
	{/if}
</section>

{#snippet expiryNote()}
	{#if minted}
		<p class="expiry">{m('org.partnerLink.expiresIn', { minutes: minted.expiresInMinutes })}</p>
	{/if}
{/snippet}

<SecretReveal
	open={minted !== null}
	ariaLabel={m('org.partnerLink.reveal.aria')}
	heading={m('org.partnerLink.reveal.heading')}
	warningStrong={m('org.partnerLink.reveal.warningStrong')}
	warning={m('org.partnerLink.reveal.warning')}
	secret={minted?.code ?? ''}
	testId="minted-link-code"
	copyLabel={m('org.partnerLink.copy')}
	copiedLabel={m('org.partnerLink.reveal.copied')}
	copiedToast={m('org.partnerLink.codeCopied')}
	copyFailedToast={m('org.partnerLink.copyFailed')}
	doneLabel={m('org.partnerLink.reveal.done')}
	note={expiryNote}
	onclose={() => (minted = null)}
/>

<style>
	.link-code-actions {
		display: flex;
		gap: 0.5rem;
	}

	.mint-failure {
		color: var(--danger);
		font-size: 0.85rem;
		margin: 0 0 0.5rem;
	}

	.expiry {
		color: var(--text-muted);
		font-size: 0.78rem;
	}
</style>
