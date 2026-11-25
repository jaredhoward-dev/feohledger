// The employee app's sidebar pages, for `[[page:…]]` links in help prose. Its
// own module over `#lib/nav.ts` alone, so RichText can label a page link
// without importing ./content.ts — which pulls every employee guide, and the
// settings-slice keys they name, into whatever route renders help prose (the
// supplier portal's help included).

import type { MessageKey } from '#lib/i18n/messages.ts';
import { NAV } from '#lib/nav.ts';

export interface NavPage {
	href: string;
	labelKey: MessageKey;
	roles?: string[];
	permissions?: string[];
	/** The group's label key, for a page folded into a sidebar group. */
	groupKey?: MessageKey;
}

/** Every sidebar destination, in sidebar order. */
export const NAV_PAGES: NavPage[] = NAV.flatMap((e) =>
	e.kind === 'link'
		? [{ href: e.href, labelKey: e.labelKey, roles: e.roles, permissions: e.permissions }]
		: e.children.map((c) => ({ href: c.href, labelKey: c.labelKey, roles: c.roles, permissions: c.permissions, groupKey: e.labelKey }))
);

const navByHref = new Map(NAV_PAGES.map((p) => [p.href, p]));
export const navPageFor = (href: string): NavPage | undefined => navByHref.get(href);
