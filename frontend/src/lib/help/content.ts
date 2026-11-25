// The help centre's whole content, joined: every guide, the glossary, the page
// directory, and search over them. Only the /help pages import this module —
// a <HelpTip> elsewhere in the app needs nothing but a catalogue key, so the
// guide prose never ships with an ordinary page.
//
// Pure (no `$app`, no store): search takes a `label` resolver so the caller
// passes m(), and vitest can drive it with the English catalogue.

import type { MessageKey } from '#lib/i18n/messages.ts';
import { GLOSSARY } from './glossary.ts';
import { CONCEPT_GUIDES } from './guides/concepts.ts';
import { INVOICE_GUIDES } from './guides/invoices.ts';
import { PAYMENT_GUIDES } from './guides/payments.ts';
import { SETUP_GUIDES } from './guides/setup.ts';
import { START_GUIDES } from './guides/start.ts';
import { VENDOR_GUIDES } from './guides/vendors.ts';
import { navPageFor } from './navPages.ts';
import { PAGE_HELP } from './pages.ts';
import { searchHelpIn, type Label, type SearchHit as AnySearchHit } from './search.ts';
import { termHref, termKey, termShortKey } from './terms.ts';
import type { GlossaryCategory, GlossaryEntry, Guide, GuideKind, HelpRole } from './types.ts';

export { GLOSSARY, PAGE_HELP, termHref, termKey, termShortKey };
export type { GlossaryCategory, GlossaryEntry, Guide, GuideKind, HelpRole };

/** Every guide, in contents order: start, then the how-tos by area, then concepts. */
export const GUIDES: Guide[] = [
	...START_GUIDES,
	...INVOICE_GUIDES,
	...PAYMENT_GUIDES,
	...VENDOR_GUIDES,
	...SETUP_GUIDES,
	...CONCEPT_GUIDES
];

/** The concept guides, for the landing page. */
export const CONCEPT_GUIDES_LIST: Guide[] = CONCEPT_GUIDES;

export const GUIDE_KINDS: GuideKind[] = ['start', 'howto', 'concept'];

export const GUIDE_KIND_KEYS: Record<GuideKind, MessageKey> = {
	start: 'help.kind.start',
	howto: 'help.kind.howto',
	concept: 'help.kind.concept'
};

/** The how-tos, grouped as the contents list shows them. */
export const HOWTO_GROUPS: { key: MessageKey; guides: Guide[] }[] = [
	{ key: 'help.area.invoices', guides: INVOICE_GUIDES },
	{ key: 'help.area.payments', guides: PAYMENT_GUIDES },
	{ key: 'help.area.vendors', guides: VENDOR_GUIDES },
	{ key: 'help.area.setup', guides: SETUP_GUIDES }
];

export const GLOSSARY_CATEGORIES: GlossaryCategory[] = [
	'invoices',
	'matching',
	'approvals',
	'payments',
	'vendors',
	'controls',
	'platform'
];

export const CATEGORY_KEYS: Record<GlossaryCategory, MessageKey> = {
	invoices: 'help.cat.invoices',
	matching: 'help.cat.matching',
	approvals: 'help.cat.approvals',
	payments: 'help.cat.payments',
	vendors: 'help.cat.vendors',
	controls: 'help.cat.controls',
	platform: 'help.cat.platform'
};

/** The starting guide for each role, in the order the landing page offers them. */
export const ROLE_START: Record<HelpRole, string> = {
	ap_clerk: 'start-ap-clerk',
	ap_manager: 'start-ap-manager',
	cfo: 'start-cfo',
	admin: 'start-admin'
};
export const HELP_ROLES: HelpRole[] = ['ap_clerk', 'ap_manager', 'cfo', 'admin'];

const guidesById = new Map(GUIDES.map((g) => [g.id, g]));
const termsById = new Map(GLOSSARY.map((t) => [t.id, t]));

export const guideFor = (id: string): Guide | undefined => guidesById.get(id);

/**
 * The guides that explain a glossary term — every guide listing it in
 * `terms` — how-tos first, then concepts, then the role starts. What a
 * HelpTip offers as further reading.
 */
export function guidesForTerm(id: string, limit = 3): Guide[] {
	const rank: Record<GuideKind, number> = { howto: 0, concept: 1, start: 2 };
	return GUIDES.filter((g) => g.terms?.includes(id))
		.sort((a, b) => rank[a.kind] - rank[b.kind])
		.slice(0, limit);
}
export const termFor = (id: string): GlossaryEntry | undefined => termsById.get(id);


// ---------------------------------------------------------------------------
// The app's pages, for [[page:…]] links and the page directory
// ---------------------------------------------------------------------------

export { NAV_PAGES, navPageFor, type NavPage } from './navPages.ts';

export { pageHelpFor, pageHelpHref } from './routeHelp.ts';

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

export type { Label };
export type SearchHit = AnySearchHit<GlossaryEntry>;

const pageLabel = (href: string) => navPageFor(href)?.labelKey;

/**
 * Guides, glossary terms and pages matching every word of `query`, best first.
 * The scorer is `search.ts`'s, over the employee content; the supplier help
 * calls it with its own lists (decisions §517).
 */
export const searchHelp = (query: string, label: Label): SearchHit[] =>
	searchHelpIn({ guides: GUIDES, terms: GLOSSARY, pages: PAGE_HELP, pageLabel }, query, label);
