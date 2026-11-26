# Help centre — how it works and how to write for it

The in-app help lives at `/help` (signed-in, every role) and reaches into the
rest of the app in three ways: a **Help & guides** row in the sidebar footer, a
**How this page works** link in every `PageHeader`, and **ⓘ HelpTips** beside
the labels that need explaining. Reasoning: `docs/decisions.md` §244.

## What is where

| Surface | Route / file |
|---|---|
| Landing — start-here path per role (`?role=` picks one; default is the reader's most senior role), the invoice-lifecycle walkthrough, concept guides, reference cards | `routes/help/+page.svelte` |
| One guide | `routes/help/guides/[id]/+page.svelte` |
| Glossary (topic filter `?topic=`, entries anchored `#<id>`) | `routes/help/glossary/+page.svelte` |
| Page by page — every sidebar page + the account pages, marked when the reader's role can't open them | `routes/help/pages/+page.svelte` |
| Search (`?q=`, live from the shell's search box) | `routes/help/search/+page.svelte` |
| Shell — search box + contents column (folds behind a button below 1024px — `xl`, `lib/breakpoints.ts`) | `routes/help/+layout.svelte`, `components/help/HelpNav.svelte` |

Content, all under `src/lib/help/`:

| File | Holds |
|---|---|
| `types.ts` | The shapes, the inline-markup grammar, `DIAGRAM_IDS` |
| `guides/{start,invoices,payments,vendors,setup,concepts}.ts` | The guides, by area |
| `portal.ts` | The supplier portal's guides, its glossary subset and its pages (see *Supplier portal help*) |
| `glossary.ts` | Glossary ids, categories, the long English entries, aliases |
| `pages.ts` | `PAGE_HELP` — one line + a guide per sidebar href |
| `lifecycle.ts` | The invoice-lifecycle stages |
| `content.ts` | Joins it all; `searchHelp` (the scorer over the employee lists); `guidesForTerm` |
| `search.ts` | The search scorer, over whatever guides / terms / pages the caller passes — imports no content, so the supplier help can use it |
| `portalSearch.ts` | `searchPortalHelp`: the scorer over `PORTAL_GUIDES` and `PORTAL_GLOSSARY` |
| `routeHelp.ts`, `terms.ts` | The light lookups `PageHeader` / `HelpTip` use (see *Bundles* below); `terms.ts` also builds every term and guide href, per audience |
| `navPages.ts` | The sidebar pages a `[[page:…]]` link is labelled from — its own module so `RichText` never imports `content.ts` |

## Translation: two halves

- **Catalogued** (all six locales, `help.*` keys): the help centre's chrome,
  every glossary term's name (`help.term.<id>`) and one-line definition
  (`help.term.<id>.short` — the HelpTip text, ≤ 160 chars), and every word
  inside a diagram (`help.diagram.<id>.*`).
- **English prose**: guide titles, summaries and bodies, glossary long text,
  page-directory lines, lifecycle stage text. Under another UI locale it is
  rendered with `lang="en"` (WCAG 3.1.2) beneath `EnglishNotice`, which says so
  in the reader's language. Translating the prose is a tracked follow-up
  (`docs/followups.md`), not a gap to paper over by machine-translating inline.

Inside the English prose, **name UI with `{ui:key}`, never by typing the label**:
it renders the catalogue label in the reader's language (bold, with the UI's
`lang`), and a renamed or deleted key fails `content.test.ts`. A key whose value
has a `{placeholder}` can't be used (it would render the braces) — write the
label in `**bold**` instead.

## Inline markup

```
**Label**                     bold — a label with no catalogue key
*stress*                      emphasis
{ui:invoices.status.approved} the catalogue label, bold, in the reader's language
[[three-way-match]]           glossary link, labelled with the term's (translated) name
[[void|voided]]               glossary link with its own label
[[guide:run-payments|label]]  link to another guide
[[page:/payments]]            link into the app, labelled with its sidebar label
[[page:/profile|label]]       the same for a page off the sidebar (label required)
```

Rendered by `components/help/RichText.svelte` as elements — never `{@html}`.
A guide's sections go through `components/help/GuideSections.svelte`, which both
guide pages (`/help/guides/[id]`, `/portal/help/[id]`) share, as they share its
"On this page" list (`GuideToc`), its terms (`GuideTerms`) and its related
guides (`GuideRelated`) — each taking the page's `audience` where it links out.

Blocks: `p`, `steps` (numbered things to do), `list`, `note` (`tip` / `caution`
— a control that will stop you, saying *why* it exists / `role` — who may do
this), `lifecycle` (the walkthrough), `diagram` (`{ id, caption }`).

## Adding things

- **A guide** — add it to the right `guides/*.ts`, with `roles` (who it's for;
  drives the landing page's per-role task list), `route` (the page the work
  happens on — its "Open …" button is shown only if the reader's role can open
  it, via `nav.ts::canSee`), `terms` and `related`. Its id is its URL: never
  rename one that has shipped.
- **A glossary term** — an entry in `glossary.ts` **and** `help.term.<id>` +
  `help.term.<id>.short` in all six locale files. Add the id to some guide's
  `terms` so the term's HelpTip has further reading (the test requires it for
  every tipped term).
- **A sidebar page** — `PAGE_HELP` must gain its href in the same change
  (`content.test.ts` enumerates `NAV`); that row is also what lights up the
  page's "How this page works" link.
- **An invoice status** — place it in a `lifecycle.ts` stage (or as a branch);
  the test enumerates `INVOICE_STATUSES`.
- **A diagram** — a component in `components/help/diagrams/`, an id in
  `DIAGRAM_IDS`, a lazy `import()` in `Diagram.svelte`'s `DIAGRAMS` map, and a
  `diagram` block in at least one guide. Inline SVG coloured from the `app.css` tokens (it follows the
  tenant's accent), every word from the catalogue, legible at 320px.

## Supplier portal help

Suppliers cannot open `/help` (a vendor user has no employee session), so the
portal has its own at **`/portal/help`** — a **Help** link in the portal nav,
the supplier guides and a short glossary on one page, and
`/portal/help/<id>` per guide. Reasoning: `docs/decisions.md` §508.

- **Same shape, `audience: 'supplier'`.** A supplier guide is a `Guide` in
  `lib/help/portal.ts` (`PORTAL_GUIDES`), with the same prose grammar and the
  `{ui:key}` rule. It is **not** in `GUIDES`: the employee landing, search,
  page directory and HelpTips never show it.
- **Its links stay in the portal.** `RichText audience="supplier"` sends
  `[[term]]` to `/portal/help#<id>` and `[[guide:…]]` to `/portal/help/<id>`.
  A term must be in `PORTAL_GLOSSARY` (a subset of `glossary.ts` whose
  translated one-liner reads true for a supplier — the page shows the name and
  one-liner, never the employee long text); a guide must be a supplier guide;
  a `[[page:…]]` must be a `/portal` route **with a label**; `route` must be a
  key of `PORTAL_PAGES`. No `lifecycle` block (it needs the employee index).
- **Bundle.** Nothing `/portal/help` imports may reach `content.ts`,
  `glossary.ts`, `guides/` or `lifecycle.ts`: that would ship every employee
  guide to suppliers and make the route load the settings slice. The route
  loads only the help slice (the portal slice comes from `/portal`).
- **Coverage.** Every `PORTAL_PAGES` page but `/portal/notifications` (two
  checkboxes, covered by a tip) is some guide's `route`, so a new portal page
  fails `content.test.ts` until it has a guide. Nine guides today: getting
  started, submit / resubmit, track to payment, bank details, card payments,
  invoice from a PO, early-payment offers, W-9 / W-8, invoice conversations.
- **Search.** A search field at the top of `/portal/help` narrows the guides
  and the glossary in place, best match first, through `portalSearch.ts` →
  `search.ts` (the help centre's scorer, which takes its lists as arguments).
  A term matches on its translated name and one-liner only — aliases and the
  long text live in `glossary.ts`. The count (or the no-match message) is a
  `role="status"` line that is always in the document; no match shows a
  **Clear search** button. The text is kept in `?q=` (`replaceState`, one
  history entry). Filtering in the browser is right here, unlike a list
  page's search: every guide is already in the bundle. Reasoning:
  `docs/decisions.md` §517.
- **Writing one.** Check every step against the portal route it describes,
  the endpoint behind it in `backend/app/api/portal.py`, and
  `backend/docs/supplier-portal.md` (plus the feature's own doc) — suppliers
  have no AP colleague to ask when a guide is wrong. Name portal controls with
  `{ui:portal.…}` (or the shared component's own keys, e.g.
  `{ui:supplierChat.send}`). Add a glossary term only when its translated
  one-liner reads true for a supplier: `purchase-order` and `form-w9` are
  written in the buyer's voice ("your order to a supplier"), so the PO and
  tax-form guides carry no term.

## HelpTips in the app

`<HelpTip term="three-way-match" />` — an ⓘ toggletip (click / Enter / Space,
Escape closes, a polite live region announces it) showing the term's name and
one-liner, a glossary link, and up to three guides that list the term in their
`terms`. Placement rules (`docs/ui-patterns.md` § Contextual help):

- Beside the text, never inside a `<label>`, `<button>`, `<a>`, `<legend>` or a
  sortable header — it would change that element's accessible name.
- For a heading, wrap heading + tip in a row; leave the heading's own text alone.
- A handful per page where the "what does this mean?" is real — not every label.
- A component that renders its own title can take a `helpTerm` prop and forward
  it (`ui/ApprovalChainProgress.svelte`); the test reads those call sites.

`term` must be a literal id, or an expression whose possible results are
literal ids (the invoice modal picks two-, three- or four-way by match type).

## Bundles

`PageHeader` and `HelpTip` render on ordinary pages, so neither may import
`content.ts` (which pulls every guide): `PageHeader` reads `routeHelp.ts` (the
small page directory only), and `HelpTip` needs only catalogue keys
(`terms.ts`), fetching the guide index with a dynamic `import()` the first time
any tip opens. `content.test.ts` § bundle boundaries pins both.

Each guide diagram is its own lazy chunk (`Diagram.svelte`'s `DIAGRAMS` map of
dynamic imports): a guide shows one or two of the eight, and bundled together
they were ~12 KB on `/help/guides/[id]`, the route the web bundle budget gates.
The guide page's `+page.ts` awaits `loadDiagrams` for the ids its guide uses, so
SvelteKit has the drawing before the first frame and the a11y specs that wait
on the guide's `<h1>` still measure it drawn.

The glossary, help-route and diagram **strings** are their own lazy catalogue
slice too (`src/lib/i18n/locales/help/<locale>.ts`, decisions §261): the /help
`+layout.ts` awaits `ensureSlices('help', 'settings')` before rendering, and
each `HelpTip` calls `ensureSlices('help')` as it initialises, showing the
static English copy until the reader's slice lands. Add a new `help.*` key
there, in all six locales — only `help.pageLink` and `help.tip.*` stay in the
main catalogue.

/help loads the **settings** slice as well (decisions §300) because guides name
the configuration screens' controls with `{ui:org.…}` / `{ui:admin.…}`, and
those keys now live in it. A guide that starts naming the supplier portal's
controls (`{ui:portal.…}`) needs `'portal'` added to that load —
`src/lib/i18n/slices.test.ts` fails until it is, naming the guide.

## Tests

- `src/lib/help/content.test.ts` — every reference in every piece of prose
  resolves (`{ui:}` keys exist and take no params; terms, guides and routes
  exist; unlabelled page links are sidebar entries); glossary complete in all
  six locales; every `<HelpTip>` term exists and has further reading; the page
  directory covers the sidebar; every invoice status is in the lifecycle;
  every diagram is drawn and used; the bundle boundaries; search.
  The **supplier portal help** block holds `PORTAL_GUIDES` to the same
  reference checks plus the portal-only rules above (coverage included), and
  pins that the portal help pages never import the employee guide index.
- `src/lib/help/search.test.ts` — the scorer (`search.ts`) on its own and over
  both callers' lists: `searchHelp` is the scorer over the employee content,
  `searchPortalHelp` finds supplier guides and portal terms (and never an
  employee one). It also walks the **whole static import graph** of both
  `/portal/help` pages and fails if it reaches `content.ts`, `glossary.ts`,
  `guides/` or `lifecycle.ts` — a direct-import check would miss a component
  that pulls one in.
- `src/lib/help/inline.test.ts` — the markup parser, the page-help lookup, anchors.
- `tests-e2e/help/help-centre.spec.ts` — the pages render, search, the
  role-chips, a guide's open-page button, the PageHeader link, a HelpTip by
  keyboard. `tests-e2e/a11y/reflow.spec.ts` picks the `/help` routes up off
  disk.
- `tests-e2e/portal/help.spec.ts` — a supplier reaches `/portal/help` from the
  nav, opens a guide, follows a term link to the portal glossary and the
  guide's open-page button; the purchase-order guide renders its row button's
  label and opens `/portal/purchase-orders`; the search field narrows the page,
  announces its count, survives a reload via `?q=` and clears from its empty
  state. `tests-e2e/a11y/axe.spec.ts` scans the landing and
  a guide; `tests-e2e/portal/reflow.spec.ts` checks both at 390px.
