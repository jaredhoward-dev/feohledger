<script lang="ts">
	/**
	 * A guide's "On this page" list — one link per section heading, to the
	 * anchor `GuideSections` gives it. Shared by the help centre's guide page
	 * and the supplier portal's (docs/decisions.md §508). Rendered only when a
	 * guide has more than one section; the caller places it (the employee page
	 * turns it into a sticky rail on a wide column by styling `.toc`).
	 *
	 * No `audience` prop: every link is an in-page anchor, the same for both.
	 */
	import type { GuideSection } from '#lib/help/types.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { headingId } from '#lib/help/anchor.ts';

	let {
		sections,
		englishLang
	}: {
		sections: GuideSection[];
		/** `en` when the UI locale is not English: the headings are English (WCAG 3.1.2). */
		englishLang?: string;
	} = $props();
</script>

{#if sections.length > 1}
	<nav class="toc" aria-label={m('help.guide.onThisPage')}>
		<h2>{m('help.guide.onThisPage')}</h2>
		<ol>
			{#each sections as s (s.heading)}
				<li><a href="#{headingId(s.heading)}" lang={englishLang}>{s.heading}</a></li>
			{/each}
		</ol>
	</nav>
{/if}

<style>
	.toc {
		margin-top: 24px;
		padding: 14px 18px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--surface);
	}
	.toc h2 {
		margin: 0 0 6px;
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.toc ol {
		margin: 0;
		padding-left: 1.2rem;
	}
	.toc li {
		padding: 3px 0;
	}
	.toc a {
		color: var(--accent);
	}
</style>
