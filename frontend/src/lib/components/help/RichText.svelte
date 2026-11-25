<script lang="ts">
	/**
	 * Help prose with its small inline markup (`#lib/help/types.ts`) rendered as
	 * elements: bold, emphasis, a UI label from the catalogue, and links to a
	 * glossary term, a guide or an app page. Never `{@html}` — every part binds
	 * as text inside an element this component wrote.
	 *
	 * A `{ui:key}` label and a term's link text come from m(), so under a
	 * non-English locale they read in the reader's language even though the
	 * prose around them is English; each is marked with the UI's own `lang`
	 * so a screen reader switches voice at the boundary (WCAG 3.1.2).
	 *
	 * `audience` decides where a glossary or guide link goes: the supplier
	 * portal's help renders supplier prose, whose readers cannot open `/help`.
	 * It imports only the light lookups (`terms.ts`, `navPages.ts`), never
	 * `content.ts`, so rendering prose does not pull every employee guide into
	 * the route that does it.
	 */
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import type { HelpAudience } from '#lib/help/types.ts';
	import { m, currentLocale } from '#lib/i18n/store.svelte.ts';
	import { inline } from '#lib/help/inline.ts';
	import { navPageFor } from '#lib/help/navPages.ts';
	import { guideHref, termHref, termKey } from '#lib/help/terms.ts';

	let { text, audience = 'employee' }: { text: string; audience?: HelpAudience } = $props();

	const parts = $derived(inline(text));
	// The prose is English; the labels m() fills in are the UI's language.
	const uiLang = $derived(currentLocale() === 'en' ? undefined : currentLocale());
</script>

{#each parts as part, i (i)}{#if part.kind === 'strong'}<strong>{part.text}</strong
		>{:else if part.kind === 'em'}<em>{part.text}</em
		>{:else if part.kind === 'ui'}<strong class="ui-label" lang={uiLang}>{m(part.key as MessageKey)}</strong
		>{:else if part.kind === 'term'}<a href={termHref(part.id, audience)} lang={part.label ? undefined : uiLang}
			>{part.label ?? m(termKey(part.id))}</a
		>{:else if part.kind === 'guide'}<a href={guideHref(part.id, audience)}>{part.label}</a
		>{:else if part.kind === 'page'}{@const nav = navPageFor(part.href)}<a
			href={part.href}
			lang={part.label ? undefined : uiLang}>{part.label ?? (nav ? m(nav.labelKey) : part.href)}</a
		>{:else}{part.text}{/if}{/each}
