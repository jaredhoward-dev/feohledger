<script lang="ts">
	/**
	 * The "Related guides" block: title (linking to the guide in the help that
	 * audience can open, `guideHref`) and summary of each guide to read next.
	 * Shared by the help centre's and the supplier portal's guide pages
	 * (docs/decisions.md §508). The caller resolves the ids against its own
	 * guide index — a supplier's page must never load the employee one — and
	 * an empty list renders nothing.
	 */
	import type { Guide, HelpAudience } from '#lib/help/types.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { guideHref } from '#lib/help/terms.ts';

	let {
		guides,
		englishLang,
		audience = 'employee'
	}: {
		guides: Pick<Guide, 'id' | 'title' | 'summary'>[];
		/** `en` when the UI locale is not English: titles and summaries are English (WCAG 3.1.2). */
		englishLang?: string;
		audience?: HelpAudience;
	} = $props();
</script>

{#if guides.length}
	<section class="aside-block" aria-labelledby="related-h">
		<h2 id="related-h">{m('help.guide.related')}</h2>
		<ul class="related">
			{#each guides as g (g.id)}
				<li>
					<a href={guideHref(g.id, audience)} lang={englishLang}>{g.title}</a>
					<p lang={englishLang}>{g.summary}</p>
				</li>
			{/each}
		</ul>
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
	.related {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.related li {
		padding: 10px 0;
		border-bottom: 1px solid var(--border);
	}
	.related a {
		font-weight: 600;
		color: var(--accent);
	}
	.related p {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
</style>
