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
	 */
	import LinkedMessage from '#lib/components/ui/LinkedMessage.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { legalTitle } from '#lib/legal/pages.ts';

	let {
		checked = $bindable(false),
		id,
		error = '',
		withDpa = false,
		onchange
	}: {
		checked?: boolean;
		id: string;
		/** Shown beside the box, and announced, when a submit found it unticked. */
		error?: string;
		withDpa?: boolean;
		onchange?: () => void;
	} = $props();

	let input: HTMLInputElement | undefined = $state();

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
		required
		aria-invalid={error ? 'true' : undefined}
		aria-describedby={error ? `${id}-error` : undefined}
	/>
	<label for={id}>
		<LinkedMessage
			text={m(withDpa ? 'auth.signup.legalConsent' : 'auth.legal.acceptLabel')}
			links={{
				terms: { href: '/legal/terms', label: legalTitle('/legal/terms') },
				privacy: { href: '/legal/privacy', label: legalTitle('/legal/privacy') },
				...(withDpa ? { dpa: { href: '/legal/dpa', label: legalTitle('/legal/dpa') } } : {})
			}}
		/>
	</label>
</div>
{#if error}
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
</style>
