<script lang="ts">
	/**
	 * One guide: breadcrumb, title and summary, who it is for, a button into
	 * the page the work happens on (only when the reader's role can open it —
	 * the same `canSee` gate the sidebar uses, so help never offers a page that
	 * would 403), an "On this page" list, the sections, then the terms it uses
	 * and the guides to read next.
	 *
	 * The prose is English (`lang="en"` under another locale, behind
	 * EnglishNotice); the chrome around it and every `{ui:…}` label inside it
	 * are in the reader's language.
	 */
	import { page } from '$app/state';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { brand } from '#lib/stores/brand.svelte.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { canSee } from '#lib/nav.ts';
	import { ROLE_LABEL_KEYS } from '#lib/types/admin.ts';
	import {
		GUIDE_KIND_KEYS,
		guideFor,
		navPageFor,
		termFor
	} from '#lib/help/content.ts';
	import HelpCrumbs from '#lib/components/help/HelpCrumbs.svelte';
	import EnglishNotice from '#lib/components/help/EnglishNotice.svelte';
	import InvoiceLifecycle from '#lib/components/help/InvoiceLifecycle.svelte';
	import GuideSections from '#lib/components/help/GuideSections.svelte';
	import GuideToc from '#lib/components/help/GuideToc.svelte';
	import GuideTerms from '#lib/components/help/GuideTerms.svelte';
	import GuideRelated from '#lib/components/help/GuideRelated.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';

	const guide = $derived(guideFor(page.params.id ?? ''));
	// A breadcrumb lands where its words say: the landing page's section for
	// that kind of guide (role starts and their task lists share "Start here").
	const KIND_ANCHORS = { start: '/help#start-h', howto: '/help#start-h', concept: '/help#how-h' } as const;
	const englishLang = $derived(currentLocale() === 'en' ? undefined : 'en');

	const has = (...roles: string[]) => auth.hasAnyRole(...roles);
	const can = (perm: string) => auth.can(perm);
	const target = $derived.by(() => {
		if (!guide?.route) return undefined;
		const nav = navPageFor(guide.route);
		if (nav && !canSee(nav.roles, has, nav.permissions, can)) return undefined;
		return { href: guide.route, label: nav ? m(nav.labelKey) : undefined };
	});
	const terms = $derived((guide?.terms ?? []).filter((id) => termFor(id)));
	const related = $derived((guide?.related ?? []).flatMap((id) => guideFor(id) ?? []));
</script>

<svelte:head>
	<title>{guide?.title ?? m('help.guide.notFoundTitle')} · {m('help.title')} · {brand.productName}</title>
</svelte:head>

{#if !guide}
	<HelpCrumbs trail={[]} current={m('help.guide.notFoundTitle')} />
	<h1 class="title">{m('help.guide.notFoundTitle')}</h1>
	<p>{m('help.guide.notFound')}</p>
	<p><a href="/help">{m('help.guide.backToHelp')}</a></p>
{:else}
	{#key guide.id}
		<article class="guide" data-testid="help-guide">
			<HelpCrumbs trail={[{ href: KIND_ANCHORS[guide.kind], label: m(GUIDE_KIND_KEYS[guide.kind]) }]} current={guide.title} currentLang={englishLang} />
			<header>
				<h1 class="title" lang={englishLang}>{guide.title}</h1>
				<p class="summary" lang={englishLang}>{guide.summary}</p>
				<div class="meta">
					<span class="for">
						<span class="meta-label">{m('help.guide.for')}</span>
						{#if guide.roles?.length}
							{#each guide.roles as r (r)}<Badge tone="muted">{m(ROLE_LABEL_KEYS[r])}</Badge>{/each}
						{:else}
							<Badge tone="muted">{m('help.guide.everyone')}</Badge>
						{/if}
					</span>
					{#if target}
						<a class="btn-primary open" href={target.href} data-testid="help-open-page">
							{target.label ? m('help.guide.open', { page: target.label }) : m('help.guide.openPage')}
						</a>
					{/if}
				</div>
				<EnglishNotice />
			</header>

			<div class="guide-grid" class:has-toc={guide.sections.length > 1}>
			<GuideToc sections={guide.sections} {englishLang} />

			<div class="body" lang={englishLang}>
				<GuideSections sections={guide.sections} {englishLang}>
					{#snippet lifecycle()}<InvoiceLifecycle />{/snippet}
				</GuideSections>
			</div>
			</div>

			<GuideTerms {terms} />
			<GuideRelated guides={related} {englishLang} />
		</article>
	{/key}
{/if}

<style>
	/* Header and closing sections share the text column's measure; on a
	   wide help column the "On this page" list becomes a sticky rail to its
	   right instead of a box above the text, so the width is used for
	   navigation rather than left empty. */
	.guide > :global(*) {
		max-width: 50rem;
	}
	.guide > .guide-grid {
		max-width: none;
	}
	.guide-grid {
		display: grid;
		grid-template-columns: minmax(0, 50rem);
	}
	@container help-main (min-width: 68rem) {
		.guide-grid.has-toc {
			grid-template-columns: minmax(0, 50rem) 15rem;
			column-gap: 48px;
			align-items: start;
		}
		.guide-grid.has-toc :global(.toc) {
			grid-column: 2;
			grid-row: 1;
			position: sticky;
			top: 20px;
			margin-top: 32px;
		}
		.guide-grid.has-toc .body {
			grid-column: 1;
			grid-row: 1;
		}
	}
	.title {
		margin: 8px 0 8px;
		font-size: clamp(1.5rem, 1.2rem + 1vw, 2rem);
		font-weight: 800;
		letter-spacing: -0.02em;
		line-height: 1.2;
	}
	.summary {
		margin: 0;
		color: var(--text-muted);
		font-size: 1.05rem;
		line-height: 1.55;
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-top: 16px;
	}
	.for {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.meta-label {
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.open {
		text-decoration: none;
	}
	.body {
		line-height: 1.65;
	}
</style>
