<script lang="ts">
	/**
	 * Organization → Data residency (`settings.residency.region`, see
	 * docs/data-residency.md). Nothing here blocks; the region never moves
	 * data by itself.
	 *
	 * `region` is the page's bound select value, so an unsaved choice survives
	 * a panel switch; everything else is what the server last served. The
	 * page owns the load, the save (which snaps the select back on a refusal)
	 * and the load-failure branch outside the read-only fieldset, which
	 * renders the same `intro`.
	 */
	import type { Snippet } from 'svelte';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import type { ResidencyAlignment } from './panelState.ts';

	let {
		region = $bindable(),
		intro,
		loading,
		savedRegion,
		defaultRegion,
		regions,
		placement,
		alignment,
		saving,
		onsave
	}: {
		region: string;
		/** The heading + hint, shared with the page's load-failure branch. */
		intro: Snippet;
		loading: boolean;
		/** The last persisted effective region. */
		savedRegion: string;
		defaultRegion: string;
		regions: string[];
		placement: Record<string, string>;
		alignment: ResidencyAlignment | null;
		saving: boolean;
		onsave: () => void;
	} = $props();

	// Region tokens come from the server; their display names are ours.
	const REGION_LABEL_KEYS: Record<string, MessageKey> = {
		us: 'org.residency.region.us',
		eu: 'org.residency.region.eu',
		uk: 'org.residency.region.uk',
		ca: 'org.residency.region.ca',
		au: 'org.residency.region.au'
	};

	// An unmapped token renders as itself rather than vanishing — the server
	// owns the supported set, so a region added there stays selectable here.
	function regionLabel(token: string | null): string {
		if (!token) return '';
		const key = REGION_LABEL_KEYS[token];
		return key ? m(key) : token.toUpperCase();
	}

	const alignmentMessage = $derived.by(() => {
		const a = alignment;
		if (!a) return '';
		if (a.status === 'aligned') {
			return m('org.residency.alignment.aligned', { region: regionLabel(a.deployed_region) });
		}

		if (a.status === 'misaligned') {
			return m('org.residency.alignment.misaligned', {
				configured: regionLabel(savedRegion),
				deployed: regionLabel(a.deployed_region)
			});
		}

		return a.reason === 'deployed_region_unrecognised'
			? m('org.residency.alignment.unknownUnrecognised')
			: m('org.residency.alignment.unknownUnset');
	});
</script>

<section class="card org-panel">
	{@render intro()}

	{#if loading}
		<LoadingState label={m('org.residency.loading')} />
	{:else}
		<div class="form-grid">
			<label>
				<span>{m('org.residency.regionLabel')}</span>
				<select bind:value={region}>
					{#each regions as token (token)}
						<option value={token}>
							{token === defaultRegion
								? m('org.residency.regionDefault', { region: regionLabel(token) })
								: regionLabel(token)}
						</option>
					{/each}
				</select>
			</label>
		</div>

		{#if placement.db_cluster}
			<p class="card-hint residency-placement">
				{m('org.residency.placement', {
					cluster: placement.db_cluster,
					bucket: placement.s3_bucket ?? ''
				})}
			</p>
		{/if}

		{#if alignment}
			<div
				class="residency-alignment"
				class:ok={alignment.status === 'aligned'}
				class:warn={alignment.status === 'misaligned'}
			>
				<strong>{m('org.residency.alignment.title')}</strong>
				<p>{alignmentMessage}</p>
				<p class="residency-advisory">{m('org.residency.alignment.advisory')}</p>
			</div>
		{/if}

		<div class="section-footer">
			<button
				class="btn-save-section"
				disabled={saving || region === savedRegion}
				onclick={onsave}
			>
				{saving ? m('org.common.saving') : m('org.residency.save')}
			</button>
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

	.card-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 14px;
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

	.residency-placement {
		font-family: var(--font-mono);
		margin: 12px 0 0;
	}

	/* Advisory verdict box. Tinted-badge recipe: each tone takes its own
	   -tint background with the matching -on-tint text (never the base token,
	   which lands under 4.5:1 once composited over the tint). Unknown — the
	   deliberately non-committal state — is the muted default. */
	.residency-alignment {
		margin-top: 14px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	.residency-alignment.ok {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.residency-alignment.warn {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.residency-alignment strong {
		display: block;
		margin-bottom: 4px;
	}

	.residency-alignment p {
		margin: 0;
	}

	/* Inherits the box's calibrated colour — a muted token here would be
	   judged against the bare surface, not the tint it actually sits on. */
	.residency-advisory {
		margin-top: 6px;
		font-size: 0.78rem;
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
