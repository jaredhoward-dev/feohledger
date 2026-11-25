<script lang="ts">
	/**
	 * One supplier guide (decisions §509): the same `Guide` shape and section
	 * renderer as the help centre's guide page (`GuideSections`), with its
	 * links kept inside the portal (`audience="supplier"`). No role badges —
	 * every portal user is a supplier — and the "Open …" button is always
	 * shown, since a supplier can open every portal page.
	 */
	import { page } from '$app/state';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { PORTAL_PAGES, portalGuideFor } from '#lib/help/portal.ts';
	import { guideHref, termHref, termKey, termShortKey } from '#lib/help/terms.ts';
	import { headingId } from '#lib/help/anchor.ts';
	import HelpCrumbs from '#lib/components/help/HelpCrumbs.svelte';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';
	import GuideSections from '#lib/components/help/GuideSections.svelte';

	const guide = $derived(portalGuideFor(page.params.id ?? ''));
	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');
	const home = $derived({ href: '/portal/help', label: m('help.title') });
	const target = $derived(guide?.route && PORTAL_PAGES[guide.route] ? { href: guide.route, label: m(PORTAL_PAGES[guide.route]) } : undefined);
	const related = $derived((guide?.related ?? []).flatMap((id) => portalGuideFor(id) ?? []));
</script>

{#if !guide}
	<div class="guide">
		<HelpCrumbs {home} trail={[]} current={m('help.guide.notFoundTitle')} />
		<h1 class="title">{m('help.guide.notFoundTitle')}</h1>
		<p>{m('help.guide.notFound')}</p>
		<p><a href="/portal/help">{m('help.guide.backToHelp')}</a></p>
	</div>
{:else}
	{#key guide.id}
		<article class="guide" data-testid="portal-help-guide">
			<HelpCrumbs {home} trail={[]} current={guide.title} currentLang={englishLang} />
			<header>
				<h1 class="title" lang={englishLang}>{guide.title}</h1>
				<p class="summary" lang={englishLang}>{guide.summary}</p>
				{#if target}
					<p class="open-row">
						<a class="btn-primary open" href={target.href} data-testid="portal-help-open-page">
							{m('help.guide.open', { page: target.label })}
						</a>
					</p>
				{/if}
				<EnglishNotice />
			</header>

			{#if guide.sections.length > 1}
				<nav class="toc" aria-label={m('help.guide.onThisPage')}>
					<h2>{m('help.guide.onThisPage')}</h2>
					<ol>
						{#each guide.sections as s (s.heading)}
							<li><a href="#{headingId(s.heading)}" lang={englishLang}>{s.heading}</a></li>
						{/each}
					</ol>
				</nav>
			{/if}

			<div class="body" lang={englishLang}>
				<GuideSections sections={guide.sections} {englishLang} audience="supplier" />
			</div>

			{#if guide.terms?.length}
				<section class="aside-block" aria-labelledby="terms-h">
					<h2 id="terms-h">{m('help.guide.terms')}</h2>
					<dl class="terms">
						{#each guide.terms as id (id)}
							<div>
								<dt><a href={termHref(id, 'supplier')}>{m(termKey(id))}</a></dt>
								<dd>{m(termShortKey(id))}</dd>
							</div>
						{/each}
					</dl>
				</section>
			{/if}

			{#if related.length}
				<section class="aside-block" aria-labelledby="related-h">
					<h2 id="related-h">{m('help.guide.related')}</h2>
					<ul class="related">
						{#each related as g (g.id)}
							<li>
								<a href={guideHref(g.id, 'supplier')} lang={englishLang}>{g.title}</a>
								<p lang={englishLang}>{g.summary}</p>
							</li>
						{/each}
					</ul>
				</section>
			{/if}
		</article>
	{/key}
{/if}

<style>
	.guide {
		max-width: 50rem;
	}
	.title {
		margin: 8px 0 8px;
		font-size: clamp(1.5rem, 1.2rem + 1vw, 2rem);
		font-weight: 800;
		line-height: 1.2;
	}
	.summary {
		margin: 0;
		color: var(--text-muted);
		font-size: 1.05rem;
		line-height: 1.55;
	}
	.open-row {
		margin: 16px 0 0;
	}
	.open {
		text-decoration: none;
	}
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
	.toc a,
	.terms a,
	.related a {
		color: var(--accent);
	}
	.body {
		line-height: 1.65;
	}
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
	.terms dd {
		margin: 2px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
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
	}
	.related p {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
</style>
