<script lang="ts">
	/**
	 * One supplier guide (decisions §508): the same `Guide` shape and section
	 * renderer as the help centre's guide page (`GuideSections`), with its
	 * links kept inside the portal (`audience="supplier"`). No role badges —
	 * every portal user is a supplier — and the "Open …" button is always
	 * shown, since a supplier can open every portal page.
	 */
	import { page } from '$app/state';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { PORTAL_PAGES, portalGuideFor } from '#lib/help/portal.ts';
	import HelpCrumbs from '#lib/components/help/HelpCrumbs.svelte';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';
	import GuideSections from '#lib/components/help/GuideSections.svelte';
	import GuideToc from '#lib/components/help/GuideToc.svelte';
	import GuideTerms from '#lib/components/help/GuideTerms.svelte';
	import GuideRelated from '#lib/components/help/GuideRelated.svelte';

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

			<GuideToc sections={guide.sections} {englishLang} />

			<div class="body" lang={englishLang}>
				<GuideSections sections={guide.sections} {englishLang} audience="supplier" />
			</div>

			<GuideTerms terms={guide.terms ?? []} audience="supplier" />
			<GuideRelated guides={related} {englishLang} audience="supplier" />
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
	.body {
		line-height: 1.65;
	}
</style>
