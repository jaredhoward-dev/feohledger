<script lang="ts">
	/**
	 * Help for supplier-portal users (decisions §508): the supplier guides,
	 * then a short glossary whose entries are the anchors a guide's
	 * `[[term]]` links land on (`termHref(id, 'supplier')` → `/portal/help#<id>`).
	 *
	 * Guide titles and summaries are English prose (`lang="en"` under another
	 * locale, behind EnglishNotice); the chrome, the term names and their
	 * one-line definitions are catalogue text in the reader's language.
	 *
	 * The search field (decisions §517) narrows both lists in place, best
	 * match first, through `portalSearch.ts` — never content.ts, which would
	 * ship every employee guide to suppliers. The text is kept in `?q=` so a
	 * reload or a shared link keeps the search; it replaces the history entry
	 * rather than adding one per keystroke.
	 */
	import { afterNavigate, replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { PORTAL_GLOSSARY, PORTAL_GUIDES } from '#lib/help/portal.ts';
	import { searchPortalHelp } from '#lib/help/portalSearch.ts';
	import { guideHref, termKey, termShortKey } from '#lib/help/terms.ts';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';
	import SearchBox from '#lib/components/ui/SearchBox.svelte';

	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');

	let q = $state(page.url.searchParams.get('q') ?? '');
	const query = $derived(q.trim());
	const hits = $derived(query ? searchPortalHelp(query, m) : null);
	const guides = $derived(hits ? hits.flatMap((h) => (h.kind === 'guide' ? [h.guide] : [])) : PORTAL_GUIDES);
	const terms = $derived(hits ? hits.flatMap((h) => (h.kind === 'term' ? [h.term.id] : [])) : PORTAL_GLOSSARY);

	// The lists filter on every keystroke; the count sentence in the live
	// region settles first. Announcing each intermediate count ("3 results for
	// p", "1 result for pa", …) queues a screen reader behind the typist, so
	// the region takes the sentence only once typing pauses. Clearing is
	// immediate: a stale "no results" must not outlive the search.
	const STATUS_SETTLE_MS = 300;
	const statusText = $derived(
		hits ? (hits.length ? m('portal.help.searchResults', { n: hits.length, q: query }) : m('portal.help.searchNone', { q: query })) : ''
	);
	let announced = $state('');
	$effect(() => {
		const text = statusText;
		if (!text) {
			announced = '';
			return;
		}
		const timer = setTimeout(() => (announced = text), STATUS_SETTLE_MS);
		return () => clearTimeout(timer);
	});

	// A navigation to this same page (the portal nav's Help link while a search
	// is showing, Back / Forward) brings its own `?q=`. The shallow
	// replaceState below fires no afterNavigate, so it cannot loop back here.
	afterNavigate(({ from, to }) => {
		if (from?.route.id === to?.route.id) q = to?.url.searchParams.get('q') ?? '';
	});

	function syncUrl(value: string) {
		const url = new URL(page.url.href);
		const v = value.trim();
		if (v) url.searchParams.set('q', v);
		else url.searchParams.delete('q');
		replaceState(url, page.state);
	}

	function clearSearch() {
		q = '';
		syncUrl('');
	}
</script>

<div class="page" data-testid="portal-help">
	<header>
		<h1>{m('help.title')}</h1>
		<p class="subtitle">{m('portal.help.subtitle')}</p>
		<EnglishNotice />
	</header>

	<div class="search" role="search">
		<SearchBox
			bind:value={q}
			oninput={syncUrl}
			ariaLabel={m('portal.help.searchLabel')}
			placeholder={m('portal.help.searchPlaceholder')}
			testid="portal-help-search"
		/>
		<!-- Always in the document, so the count is announced as it changes. -->
		<p class="status" role="status" data-testid="portal-help-search-status">{announced}</p>
		{#if hits && !hits.length}
			<button type="button" class="btn-cancel" onclick={clearSearch} data-testid="portal-help-search-clear"
				>{m('portal.help.searchClear')}</button
			>
		{/if}
	</div>

	{#if guides.length}
		<section aria-labelledby="portal-help-guides-h">
			<h2 id="portal-help-guides-h">{m('portal.help.guides')}</h2>
			<ul class="guides">
				{#each guides as g (g.id)}
					<li>
						<a href={guideHref(g.id, 'supplier')} lang={englishLang}>{g.title}</a>
						<p lang={englishLang}>{g.summary}</p>
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if terms.length}
		<section aria-labelledby="portal-help-terms-h">
			<h2 id="portal-help-terms-h">{m('portal.help.glossary')}</h2>
			<dl class="terms">
				{#each terms as id (id)}
					<div id={id}>
						<dt>{m(termKey(id))}</dt>
						<dd>{m(termShortKey(id))}</dd>
					</div>
				{/each}
			</dl>
		</section>
	{/if}
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
	.search {
		margin-top: 20px;
	}
	.status {
		margin: 8px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	.search .btn-cancel {
		margin-top: 8px;
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
	/* Tint → on-tint pairing: --text-muted is not rated on --muted-tint;
	   --muted-on-tint is (app.css). */
	.terms > div:target dd {
		color: var(--muted-on-tint);
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
