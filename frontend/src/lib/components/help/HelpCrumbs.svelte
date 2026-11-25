<script lang="ts">
	/**
	 * The breadcrumb over every help subpage: Help › <section> › <page>. The
	 * last crumb is the page itself (`aria-current`), not a link.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		home,
		trail,
		current,
		currentLang
	}: {
		/** The first crumb. Default: the help centre; the supplier portal's help passes its own. */
		home?: { href: string; label: string };
		trail: { href: string; label: string }[];
		current: string;
		/** `lang` of the current page's title, when it is English help prose. */
		currentLang?: string;
	} = $props();
</script>

<nav class="crumbs" aria-label={m('help.crumbs')}>
	<ol>
		<li><a href={home?.href ?? '/help'}>{home?.label ?? m('help.title')}</a></li>
		{#each trail as t (t.href + t.label)}
			<li><a href={t.href}>{t.label}</a></li>
		{/each}
		<li><span aria-current="page" lang={currentLang}>{current}</span></li>
	</ol>
</nav>

<style>
	ol {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin: 0;
		padding: 0;
		list-style: none;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	li:not(:last-child)::after {
		content: '›';
		margin-left: 4px;
	}
	a {
		color: var(--text-muted);
	}
	a:hover {
		color: var(--text);
	}
	[aria-current] {
		color: var(--text);
	}
</style>
