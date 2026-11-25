<script lang="ts">
	/**
	 * Organization → Custom domains (`settings.brand.custom_domains`, see
	 * docs/white-label.md § Custom domains).
	 *
	 * `newDomain` is the page's bound input, so a half-typed host survives a
	 * panel switch. The page owns the list, the add / remove writes, their
	 * refusal text, the armed remove (which a click elsewhere disarms, from
	 * the page's window handler on `.domain-remove`) and the load-failure
	 * branch outside the read-only fieldset, which renders the same `intro`.
	 */
	import type { Snippet } from 'svelte';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		newDomain = $bindable(),
		intro,
		domains,
		loading,
		saving,
		saveError,
		confirmRemove,
		onremove,
		onadd
	}: {
		newDomain: string;
		/** The heading + hints, shared with the page's load-failure branch. */
		intro: Snippet;
		domains: string[];
		loading: boolean;
		saving: boolean;
		/** A refused add / remove, kept on screen beside the field. */
		saveError: string;
		/** The host armed for a confirm-on-second-click, or `null`. */
		confirmRemove: string | null;
		onremove: (host: string) => void;
		onadd: () => void;
	} = $props();
</script>

<section class="card">
	{@render intro()}

	{#if loading}
		<LoadingState label={m('org.customDomains.loading')} />
	{:else}
		{#if saveError}
			<p class="domain-error" role="alert" data-testid="custom-domain-error">
				{saveError}
			</p>
		{/if}
		{#if domains.length === 0}
			<p class="card-hint domain-empty">{m('org.customDomains.empty')}</p>
		{:else}
			<ul class="domain-list">
				{#each domains as host (host)}
					<li class="domain-row">
						<span class="domain-name mono">{host}</span>
						<span class="domain-remove">
							<button
								type="button"
								class="btn-remove-domain"
								class:armed={confirmRemove === host}
								disabled={saving}
								aria-label={m('org.customDomains.removeAria', { host })}
								onclick={() => onremove(host)}
							>
								{confirmRemove === host ? m('org.customDomains.confirmRemove') : m('org.customDomains.remove')}
							</button>
						</span>
					</li>
				{/each}
			</ul>
		{/if}

		<form
			class="domain-add"
			onsubmit={(e) => {
				e.preventDefault();
				onadd();
			}}
		>
			<input
				type="text"
				bind:value={newDomain}
				placeholder={m('org.customDomains.newPlaceholder')}
				aria-label={m('org.customDomains.newAria')}
				autocomplete="off"
				spellcheck="false"
			/>
			<button type="submit" class="btn-save-section" disabled={saving}>
				{saving ? m('org.customDomains.adding') : m('org.customDomains.add')}
			</button>
		</form>
	{/if}
</section>

<style>
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 20px 24px;
	}

	.card-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	/* Custom domains */
	.domain-list {
		list-style: none;
		margin: 4px 0 16px;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.domain-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 8px 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
	}

	.domain-name {
		font-size: 0.9rem;
		word-break: break-all;
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

	/* An in-panel save refusal (a domain add, an SSO save). A panel's failed
	   LOAD is `ui/LoadError` instead, which carries the Retry this line had no
	   room for. */
	.domain-error {
		color: var(--danger);
		font-size: 0.88rem;
		margin: 4px 0 12px;
	}

	.domain-empty {
		margin-bottom: 12px;
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
</style>
