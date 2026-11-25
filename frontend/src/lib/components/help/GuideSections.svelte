<script lang="ts">
	/**
	 * A guide's sections — heading, then its blocks (paragraphs, steps, lists,
	 * notes, diagrams) — shared by the help centre's guide page and the supplier
	 * portal's (`routes/portal/help/[id]`), so both read the same `Guide` shape
	 * the same way (docs/decisions.md §508).
	 *
	 * The invoice-lifecycle walkthrough is a `lifecycle` snippet the employee
	 * page passes in, not an import here: it reaches the whole employee guide
	 * index, which a supplier's page must not load.
	 */
	import type { Snippet } from 'svelte';
	import type { GuideSection, HelpAudience } from '#lib/help/types.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { headingId } from '#lib/help/anchor.ts';
	import RichText from './RichText.svelte';
	import Diagram from './diagrams/Diagram.svelte';

	let {
		sections,
		englishLang,
		audience = 'employee',
		lifecycle
	}: {
		sections: GuideSection[];
		/** `en` when the UI locale is not English: the prose is English (WCAG 3.1.2). */
		englishLang?: string;
		audience?: HelpAudience;
		lifecycle?: Snippet;
	} = $props();
</script>

{#each sections as s (s.heading)}
	<section>
		<h2 id={headingId(s.heading)}>{s.heading}</h2>
		{#each s.blocks as b, i (i)}
			{#if b.type === 'p'}
				<p><RichText text={b.text} {audience} /></p>
			{:else if b.type === 'steps'}
				<ol class="steps">
					{#each b.items as item, j (j)}<li><RichText text={item} {audience} /></li>{/each}
				</ol>
			{:else if b.type === 'list'}
				<ul class="list">
					{#each b.items as item, j (j)}<li><RichText text={item} {audience} /></li>{/each}
				</ul>
			{:else if b.type === 'note'}
				<aside class="note {b.tone}">
					<span class="note-label" lang={englishLang ? currentLocale() : undefined}
						>{m(b.tone === 'tip' ? 'help.note.tip' : b.tone === 'caution' ? 'help.note.caution' : 'help.note.role')}</span
					>
					<span><RichText text={b.text} {audience} /></span>
				</aside>
			{:else if b.type === 'diagram'}
				<figure class="figure diagram-figure">
					<div lang={englishLang ? currentLocale() : undefined}><Diagram id={b.id} /></div>
					<figcaption><RichText text={b.caption} {audience} /></figcaption>
				</figure>
			{:else if b.type === 'lifecycle' && lifecycle}
				<div class="figure" lang={englishLang ? currentLocale() : undefined}>{@render lifecycle()}</div>
			{/if}
		{/each}
	</section>
{/each}

<style>
	/* Links are rendered by RichText, a child component: `:global` reaches
	   them, scoped to these sections. */
	section :global(a) {
		color: var(--accent);
	}
	section {
		margin-top: 32px;
	}
	h2 {
		margin: 0 0 10px;
		font-size: 1.2rem;
		font-weight: 700;
		scroll-margin-top: 20px;
	}
	p {
		margin: 0 0 12px;
	}
	.steps {
		margin: 0 0 14px;
		padding: 0;
		list-style: none;
		counter-reset: step;
	}
	.steps li {
		position: relative;
		padding: 2px 0 12px 42px;
		counter-increment: step;
	}
	.steps li::before {
		content: counter(step);
		position: absolute;
		left: 0;
		top: 0;
		display: grid;
		place-items: center;
		width: 28px;
		height: 28px;
		border-radius: 50%;
		background: color-mix(in srgb, var(--accent) 18%, transparent);
		color: var(--text);
		font-size: 0.85rem;
		font-weight: 700;
	}
	.list {
		margin: 0 0 14px;
		padding-left: 1.3rem;
	}
	.list li {
		padding: 2px 0;
	}
	.note {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin: 4px 0 16px;
		padding: 12px 16px;
		border-left: 3px solid var(--border);
		border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
		background: var(--surface);
	}
	.note.tip {
		border-left-color: var(--success);
	}
	.note.caution {
		border-left-color: var(--warning-on-tint);
	}
	.note.role {
		border-left-color: var(--accent);
	}
	.note-label {
		font-size: 0.75rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.figure {
		margin: 8px 0 18px;
	}
	.diagram-figure {
		padding: 18px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--surface);
	}
	.diagram-figure figcaption {
		margin-top: 12px;
		color: var(--text-muted);
		font-size: 0.875rem;
		line-height: 1.5;
	}
</style>
