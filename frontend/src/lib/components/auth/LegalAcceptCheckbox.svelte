<script lang="ts">
	/**
	 * The required "I agree to the Terms of Service and Privacy Policy" control
	 * (`docs/decisions.md` §495). Used wherever an account is formed or first
	 * entered: the signup form (with the DPA named, `withDpa`) and a set-password
	 * link. The page sends `ACCEPTED_LEGAL_VERSIONS` with the request once it is
	 * ticked; the backend refuses any other value.
	 *
	 * The sentence is translated; the link LABELS are the document titles from
	 * `lib/legal/pages.ts`, English on purpose because the documents are
	 * (`docs/decisions.md` §174). The whole sentence is the checkbox's label, so
	 * a screen reader names the control by what ticking it agrees to, and the
	 * links inside stay ordinary links.
	 *
	 * `aria-required`, not `required`: the pages validate the box themselves
	 * and show the localized `error` below; a native `required` let the
	 * browser block the submit first with its own bubble, in the browser's
	 * language, on a form without `novalidate` (/signup).
	 *
	 * `versionsChanged` is the dead end a stale tab hits: the versions sent are
	 * built into the bundle, so a tab opened before a deploy that bumped the
	 * Terms or Privacy Policy is refused (`legal_acceptance_required`) however
	 * often the box is ticked. Only a reload fetches the current documents and
	 * versions, so the message says so and offers the reload beside the box.
	 *
	 * `documents="privacy"` is the supplier portal's box (`docs/decisions.md`
	 * §499): a Supplier User is not a party to the Terms, so the box names only
	 * the Privacy Policy and says it was read, not agreed to.
	 */
	import LinkedMessage from '#lib/components/ui/LinkedMessage.svelte';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { legalTitle } from '#lib/legal/pages.ts';

	let {
		checked = $bindable(false),
		id,
		error = '',
		withDpa = false,
		documents = 'terms_privacy',
		versionsChanged = false,
		onchange
	}: {
		checked?: boolean;
		id: string;
		/** Shown beside the box, and announced, when a submit found it unticked. */
		error?: string;
		withDpa?: boolean;
		/** Which documents the box names: our Terms and Privacy Policy (an
		 *  employee account), or the Privacy Policy alone (a supplier-portal
		 *  account, §499). */
		documents?: 'terms_privacy' | 'privacy';
		/** The server refused a TICKED box: the documents changed since the page
		 *  loaded. Shown in place of `error`, with a reload action. */
		versionsChanged?: boolean;
		onchange?: () => void;
	} = $props();

	let input: HTMLInputElement | undefined = $state();
	let described = $derived(Boolean(error) || versionsChanged);
	let privacyOnly = $derived(documents === 'privacy');
	let labelKey: MessageKey = $derived(
		privacyOnly
			? 'auth.legal.privacyAckLabel'
			: withDpa
				? 'auth.signup.legalConsent'
				: 'auth.legal.acceptLabel'
	);
	let privacyLink = { href: '/legal/privacy', label: legalTitle('/legal/privacy') };
	let links = $derived(
		privacyOnly
			? { privacy: privacyLink }
			: {
					terms: { href: '/legal/terms', label: legalTitle('/legal/terms') },
					privacy: privacyLink,
					...(withDpa ? { dpa: { href: '/legal/dpa', label: legalTitle('/legal/dpa') } } : {})
				}
	);

	/** Move focus to the box (a submit that found it unticked). */
	export function focus() {
		input?.focus();
	}
</script>

<div class="legal-consent">
	<input
		bind:this={input}
		{id}
		type="checkbox"
		bind:checked
		{onchange}
		aria-required="true"
		aria-invalid={described ? 'true' : undefined}
		aria-describedby={described ? `${id}-error` : undefined}
	/>
	<label for={id}>
		<LinkedMessage text={m(labelKey)} {links} />
	</label>
</div>
{#if versionsChanged}
	<div id="{id}-error" class="hint bad versions-changed" role="alert">
		<span>{m(privacyOnly ? 'auth.legal.privacyVersionsChanged' : 'auth.legal.versionsChanged')}</span>
		<button type="button" class="reload" onclick={() => location.reload()}>
			{m('auth.legal.reload')}
		</button>
	</div>
{:else if error}
	<small id="{id}-error" class="hint bad" role="alert">{error}</small>
{/if}

<style>
	.legal-consent {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		margin: 2px 0 0;
		font-size: 0.85rem;
		line-height: 1.55;
		color: var(--text);
	}
	/* Beats AuthShell's `.form-wrap :where(label)` column flex, which would
	   otherwise stack each text run and link of the sentence as its own row. */
	.legal-consent label {
		display: block;
		flex: 1 1 auto;
		min-width: 0;
	}
	.legal-consent input {
		/* WCAG 2.5.8: a 24px target. */
		flex: none;
		width: 24px;
		height: 24px;
		margin: 0;
		accent-color: var(--accent);
	}
	.legal-consent :global(a) {
		color: var(--accent-on-tint);
	}
	.hint.bad {
		font-size: 0.8rem;
		color: var(--danger);
	}
	.versions-changed {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px 10px;
	}
	.reload {
		/* WCAG 2.5.8: a 24px target. */
		min-height: 24px;
		padding: 2px 10px;
		font: inherit;
		font-weight: 600;
		color: var(--accent-on-tint);
		background: transparent;
		border: 1px solid currentColor;
		border-radius: 6px;
		cursor: pointer;
	}
</style>
