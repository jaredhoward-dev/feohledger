<script lang="ts">
	/**
	 * The "Terms in this guide" block: each glossary term the guide uses, its
	 * name linking to the entry in the glossary that audience can open
	 * (`termHref`: `/help/glossary#…` for employees, `/portal/help#…` for
	 * suppliers) and its one-line definition. Shared by the help centre's and
	 * the supplier portal's guide pages (docs/decisions.md §508). The caller
	 * passes only ids its glossary has; an empty list renders nothing.
	 */
	import type { HelpAudience } from '#lib/help/types.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { termHref, termKey, termShortKey } from '#lib/help/terms.ts';

	let { terms, audience = 'employee' }: { terms: string[]; audience?: HelpAudience } = $props();
</script>

{#if terms.length}
	<section class="aside-block" aria-labelledby="terms-h">
		<h2 id="terms-h">{m('help.guide.terms')}</h2>
		<dl class="terms">
			{#each terms as id (id)}
				<div>
					<dt><a href={termHref(id, audience)}>{m(termKey(id))}</a></dt>
					<dd>{m(termShortKey(id))}</dd>
				</div>
			{/each}
		</dl>
	</section>
{/if}

<style>
	.aside-block {
		margin-top: 40px;
		padding-top: 24px;
		border-top: 1px solid var(--border);
	}
	.aside-block h2 {
		margin: 0 0 12px;
		font-size: 1.05rem;
		font-weight: 700;
	}
	.terms {
		display: grid;
		gap: 12px;
		margin: 0;
	}
	.terms dt {
		font-weight: 600;
	}
	.terms a {
		color: var(--accent);
	}
	.terms dd {
		margin: 2px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
</style>
