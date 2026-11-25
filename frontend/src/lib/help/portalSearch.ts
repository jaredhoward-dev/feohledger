// Search over the supplier help (/portal/help, decisions §517): `search.ts`'s
// scorer over `PORTAL_GUIDES` and the portal glossary. The portal shows each
// term's catalogue name and one-liner only, so that is all a term matches on —
// the employee entries' aliases and long text live in glossary.ts, which the
// portal must not import.

import { PORTAL_GLOSSARY, PORTAL_GUIDES, PORTAL_PAGES } from './portal.ts';
import { searchHelpIn, type Label, type SearchHit, type SearchTerm } from './search.ts';

const PORTAL_TERMS: SearchTerm[] = PORTAL_GLOSSARY.map((id) => ({ id }));

export type PortalSearchHit = Exclude<SearchHit, { kind: 'page' }>;

/** Supplier guides and portal glossary terms matching every word of `query`, best first. */
export function searchPortalHelp(query: string, label: Label): PortalSearchHit[] {
	return searchHelpIn(
		{ guides: PORTAL_GUIDES, terms: PORTAL_TERMS, pageLabel: (href) => PORTAL_PAGES[href] },
		query,
		label
	) as PortalSearchHit[];
}
