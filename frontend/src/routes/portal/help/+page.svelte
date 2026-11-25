<script lang="ts">
	/**
	 * Help for supplier-portal users (decisions §508): the supplier guides,
	 * then a short glossary whose entries are the anchors a guide's
	 * `[[term]]` links land on (`termHref(id, 'supplier')` → `/portal/help#<id>`).
	 *
	 * Guide titles and summaries are English prose (`lang="en"` under another
	 * locale, behind EnglishNotice); the chrome, the term names and their
	 * one-line definitions are catalogue text in the reader's language.
	 */
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { PORTAL_GLOSSARY, PORTAL_GUIDES } from '#lib/help/portal.ts';
	import { guideHref, termKey, termShortKey } from '#lib/help/terms.ts';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';

	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');
</script>

<div class="page" data-testid="portal-help">
	<header>
		<h1>{m('help.title')}</h1>
		<p class="subtitle">{m('portal.help.subtitle')}</p>
		<EnglishNotice />
	</header>

	<section aria-labelledby="portal-help-guides-h">
		<h2 id="portal-help-guides-h">{m('portal.help.guides')}</h2>
		<ul class="guides">
			{#each PORTAL_GUIDES as g (g.id)}
				<li>
					<a href={guideHref(g.id, 'supplier')} lang={englishLang}>{g.title}</a>
					<p lang={englishLang}>{g.summary}</p>
				</li>
			{/each}
		</ul>
	</section>

	<section aria-labelledby="portal-help-terms-h">
		<h2 id="portal-help-terms-h">{m('portal.help.glossary')}</h2>
		<dl class="terms">
			{#each PORTAL_GLOSSARY as id (id)}
				<div id={id}>
					<dt>{m(termKey(id))}</dt>
					<dd>{m(termShortKey(id))}</dd>
				</div>
			{/each}
		</dl>
	</section>
</div>

<style>
	.page {
		max-width: 50rem;
	}
	h1 {
		margin: 0 0 6px;
	}
	.subtitle {
		margin: 0;
		color: var(--text-muted);
	}
	section {
		margin-top: 28px;
	}
	h2 {
		margin: 0 0 10px;
		font-size: 1.1rem;
		font-weight: 700;
	}
	.guides {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.guides li {
		padding: 12px 0;
		border-bottom: 1px solid var(--border);
	}
	.guides a {
		color: var(--accent);
		font-weight: 600;
	}
	.guides p {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	.terms {
		display: grid;
		gap: 12px;
		margin: 0;
	}
	/* A guide's term link scrolls here; the target is marked, not just reached. */
	.terms > div {
		scroll-margin-top: 20px;
		padding: 6px 10px;
		border-radius: var(--radius-sm);
	}
	.terms > div:target {
		background: var(--muted-tint);
	}
	.terms dt {
		font-weight: 600;
	}
	.terms dd {
		margin: 2px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
</style>
