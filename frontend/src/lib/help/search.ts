// Help search: the scorer behind /help/search and the supplier help's search
// field. It imports none of the content it searches — the caller passes the
// guides, terms and pages — so /portal/help can search its own guides without
// importing content.ts, which pulls in every employee guide (decisions §508,
// §517). Keep it that way: content.test.ts walks the portal route's import
// graph and fails if it reaches content.ts, glossary.ts, guides/ or
// lifecycle.ts.
//
// Pure (no `$app`, no store): search takes a `label` resolver so the caller
// passes m(), and vitest can drive it with the English catalogue.

import type { MessageKey } from '#lib/i18n/messages.ts';
import { plainText, type InlinePart } from './inline.ts';
import { termKey, termShortKey } from './terms.ts';
import type { Guide } from './types.ts';

export type Label = (key: MessageKey) => string;

/**
 * A glossary term as search reads it. A full `GlossaryEntry` fits; so does a
 * bare `{ id }`, which matches on the term's catalogue name and one-liner only.
 */
export interface SearchTerm {
	id: string;
	/** Other names people search for. English. */
	aliases?: string[];
	/** The fuller explanation (inline markup allowed). */
	long?: string;
}

export interface HelpSearchSources<T extends SearchTerm = SearchTerm> {
	guides: readonly Guide[];
	terms: readonly T[];
	/** Page href → its help summary (inline markup). Omit when there is no page directory. */
	pages?: Readonly<Record<string, { summary: string }>>;
	/** A page's catalogue label: a page hit's title, and the text of an unlabelled `[[page:…]]` link. */
	pageLabel?: (href: string) => MessageKey | undefined;
}

export type SearchHit<T extends SearchTerm = SearchTerm> =
	| { kind: 'guide'; guide: Guide; score: number }
	| { kind: 'term'; term: T; score: number }
	| { kind: 'page'; href: string; score: number };

const norm = (s: string) =>
	s
		.normalize('NFKD')
		.replace(/[̀-ͯ]/g, '')
		.toLowerCase();

function score(words: string[], title: string, body: string): number {
	const t = norm(title);
	const b = norm(body);
	let s = 0;
	for (const w of words) {
		if (t.includes(w)) s += 3;
		else if (b.includes(w)) s += 1;
		else return 0;
	}
	if (t.startsWith(words.join(' '))) s += 5;
	return s;
}

/**
 * Guides, terms and pages matching every word of `query`, best first (a title
 * match outranks a body match; ties keep the order the sources list them in:
 * guides, then terms, then pages). Titles are matched in the reader's language
 * (term names and page labels come from the catalogue) and in English (guide
 * titles, aliases), so either finds them.
 */
export function searchHelpIn<T extends SearchTerm>(
	sources: HelpSearchSources<T>,
	query: string,
	label: Label
): SearchHit<T>[] {
	const words = norm(query).split(/\s+/).filter(Boolean);
	if (!words.length) return [];
	const pageTitle = (href: string) => {
		const key = sources.pageLabel?.(href);
		return key ? label(key) : '';
	};
	// Inline references read as the words a reader sees.
	const text = (p: Extract<InlinePart, { kind: 'ui' | 'term' | 'page' }>) =>
		p.kind === 'ui' ? label(p.key as MessageKey) : p.kind === 'term' ? label(termKey(p.id)) : pageTitle(p.href);
	const hits: (SearchHit<T> & { i: number })[] = [];
	let i = 0;
	for (const guide of sources.guides) {
		const body = [
			guide.summary,
			...guide.sections.flatMap((s) => [
				s.heading,
				...s.blocks.flatMap((b) =>
					b.type === 'p' || b.type === 'note' ? [b.text] : b.type === 'steps' || b.type === 'list' ? b.items : []
				)
			])
		]
			.map((t) => plainText(t, text))
			.join(' ');
		const s = score(words, guide.title, body);
		if (s) hits.push({ kind: 'guide', guide, score: s + 1, i: i++ });
	}
	for (const term of sources.terms) {
		const title = [label(termKey(term.id)), term.id.replace(/-/g, ' '), ...(term.aliases ?? [])].join(' ');
		const body = [label(termShortKey(term.id)), term.long ? plainText(term.long, text) : ''].join(' ');
		const s = score(words, title, body);
		if (s) hits.push({ kind: 'term', term, score: s, i: i++ });
	}
	for (const [href, help] of Object.entries(sources.pages ?? {})) {
		const s = score(words, pageTitle(href) || href, plainText(help.summary, text));
		if (s) hits.push({ kind: 'page', href, score: s, i: i++ });
	}
	return hits.sort((a, b) => b.score - a.score || a.i - b.i).map(({ i: _i, ...hit }) => hit as SearchHit<T>);
}
