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
	 * (`partner.link_code_issued`). The code is shown once and kept only in
	 * this panel's state; it is never stored or put in a URL.
	 *
	 * Admin-only end to end: `readOnly` renders the notice and makes no call.
	 */
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { mintLinkCode } from '#lib/api/partner.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';

	let {
		readOnly
	}: {
		/** The caller is not an admin: render the notice, call nothing. */
		readOnly: boolean;
	} = $props();

	let mintedCode = $state<string | null>(null);
	let mintExpiry = $state<number | null>(null);
	let minting = $state(false);

	async function mintCode() {
		minting = true;
		try {
			const res = await mintLinkCode();
			mintedCode = res.link_code;
			mintExpiry = res.expires_in_minutes;
		} catch (e) {
			toast(errorMessage(e, m('org.partnerLink.mintFailed')), 'error');
		} finally {
			minting = false;
		}
	}

	async function copyCode() {
		if (!mintedCode) return;
		try {
			await navigator.clipboard.writeText(mintedCode);
			toast(m('org.partnerLink.codeCopied'), 'success');
		} catch {
			toast(m('org.partnerLink.copyFailed'), 'error');
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
		<div class="link-code-actions">
			<button type="button" class="btn-cancel" onclick={mintCode} disabled={minting}>
				{minting ? m('org.partnerLink.generating') : m('org.partnerLink.generate')}
			</button>
		</div>
		{#if mintedCode}
			<div class="minted" data-testid="minted-link-code">
				<code class="code-value">{mintedCode}</code>
				<button type="button" class="btn-cancel copy-btn" onclick={copyCode}>
					{m('org.partnerLink.copy')}
				</button>
				{#if mintExpiry !== null}
					<span class="expiry">{m('org.partnerLink.expiresIn', { minutes: mintExpiry })}</span>
				{/if}
			</div>
		{/if}
	{/if}
</section>

<style>
	.link-code-actions {
		display: flex;
		gap: 0.5rem;
	}

	.minted {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		margin-top: 0.75rem;
		flex-wrap: wrap;
	}

	.code-value {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.78rem;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.4rem 0.6rem;
		max-width: 100%;
		overflow-wrap: anywhere;
	}

	.expiry {
		color: var(--text-muted);
		font-size: 0.78rem;
	}

	.copy-btn {
		flex: none;
	}
</style>
