import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

/**
 * `Intl.RelativeTimeFormat` is not free to construct and `timeAgo` runs once
 * per row of a notifications table, so memoize per (locale, numeric) pair.
 * The active locale is part of the key, so a locale switch never serves a
 * stale formatter.
 */
const relativeFormatters = new Map<string, Intl.RelativeTimeFormat>();

function relativeFormatter(numeric: 'auto' | 'always'): Intl.RelativeTimeFormat {
	const locale = getActiveFormatLocale();
	const key = `${locale ?? ''}|${numeric}`;
	let fmt = relativeFormatters.get(key);
	if (!fmt) {
		// `short` over `narrow`: narrow reproduces the old English exactly
		// ("5m ago") but degrades badly elsewhere — French renders a bare
		// "-5 min" and German "vor 5 m". `short` stays compact in every shipped
		// locale ("5 min. ago" / "vor 5 Min." / "il y a 5 min" / "5 分前").
		fmt = new Intl.RelativeTimeFormat(locale, { numeric, style: 'short' });
		relativeFormatters.set(key, fmt);
	}
	return fmt;
}

/**
 * Friendly relative-time formatting (e.g. "now", "5 min. ago", "2 days ago").
 * Shared by the notifications page + the sidebar notification popover so the
 * two can't drift. Coarse buckets only — exact timestamps go in `title`.
 *
 * Localized via `Intl.RelativeTimeFormat` keyed on the active in-app locale,
 * the same holder {@link formatDate} reads — so the picker moves relative
 * times alongside dates and money instead of leaving hardcoded English labels
 * inside an otherwise-German page. `Intl` was chosen over ICU plural message
 * keys because this module must stay importable under vitest's node
 * environment: `m()` lives in `i18n/store.svelte.ts`, a rune module that
 * imports `$app/env`, and pulling it in here would break both the
 * pure-module contract and the unit tests. `Intl` also gets every locale's
 * plural rules for free and needs no catalogue keys at all.
 *
 * The buckets are unchanged: under a minute, minutes, hours, then days. Only
 * the sub-minute bucket uses `numeric: 'auto'` (so it reads "now" / "jetzt"
 * rather than "in 0 seconds"); every other bucket stays explicitly numeric so
 * a day-old row can't render as "yesterday".
 *
 * A future timestamp still falls into the sub-minute bucket ("now"), matching
 * the previous behaviour. An unparseable value returns `placeholder` — the
 * old code fed `NaN` into a template literal and rendered "NaNd ago", and
 * `Intl.RelativeTimeFormat#format` throws a `RangeError` on a non-finite
 * value, which would take the whole table render down.
 */
export function timeAgo(iso: string, placeholder = '—'): string {
	const then = new Date(iso).getTime();
	if (Number.isNaN(then)) return placeholder;
	const mins = Math.floor((Date.now() - then) / 60000);
	if (mins < 1) return relativeFormatter('auto').format(0, 'second');
	if (mins < 60) return relativeFormatter('always').format(-mins, 'minute');
	const hours = Math.floor(mins / 60);
	if (hours < 24) return relativeFormatter('always').format(-hours, 'hour');
	return relativeFormatter('always').format(-Math.floor(hours / 24), 'day');
}

/**
 * A deadline relative to now, in the reader's language: "in 3 hr." / "in 2
 * days", or "3 hr. ago" once it has passed (overdue is the negative of the
 * same unit). Hours under a day, whole days after that — the buckets the
 * exception queue's Due column has always used. Reads the active locale and
 * shares {@link timeAgo}'s memoized formatter, so a table of rows constructs
 * one `Intl.RelativeTimeFormat`, not one per cell.
 *
 * Null, empty or unparseable input returns `placeholder` (default `—`).
 */
export function formatDueIn(iso: string | null | undefined, placeholder = '—'): string {
	if (!iso) return placeholder;
	const due = new Date(iso).getTime();
	if (Number.isNaN(due)) return placeholder;
	const hours = Math.round((due - Date.now()) / 3600000);
	const fmt = relativeFormatter('always');
	return Math.abs(hours) < 24
		? fmt.format(hours, 'hour')
		: fmt.format(Math.round(hours / 24), 'day');
}

/**
 * Parse a bare calendar date (`YYYY-MM-DD` or `YYYY-MM`) into a `Date` at
 * *local* midnight. `new Date('2026-06-20')` parses the string as UTC, which
 * in a negative-offset timezone (e.g. America/New_York) rolls the displayed
 * calendar day back to the 19th — so a date-only key must be split and fed to
 * the local-time `Date(y, mIndex, d)` constructor instead. Returns `null` when
 * the components aren't a real date. A value carrying a time component (an ISO
 * timestamp) is left to the normal `Date` parser, since its instant is exact.
 */
function parseLocalDate(value: string): Date | null {
	const dateOnly = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(value);
	const d = dateOnly
		? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, dateOnly[3] ? Number(dateOnly[3]) : 1)
		: new Date(value);
	return Number.isNaN(d.getTime()) ? null : d;
}

/** Default short-date parts (e.g. "Jun 20, 2026" / "20 juin 2026"). */
const DEFAULT_DATE_OPTS: Intl.DateTimeFormatOptions = {
	month: 'short',
	day: 'numeric',
	year: 'numeric'
};

/**
 * Locale-aware short calendar date (e.g. "Jun 20, 2026" / "20 juin 2026").
 *
 * The locale follows the active in-app i18n locale (the picker), read from
 * `formatLocale.ts::getActiveFormatLocale()` — the same holder `money.ts`
 * already drives `Intl.NumberFormat` off, so dates and money switch together.
 * Until a locale is actively selected the holder is `undefined`, so the
 * browser/runtime locale is used (the pre-i18n behaviour — nothing regresses).
 *
 * Pass `opts` to vary the parts a caller needs — a list row that wants no year
 * (`{month:'short', day:'numeric'}`) or a date+time (`…, hour:'numeric',
 * minute:'2-digit'}`) — while still localizing off the active locale. Omit it
 * for the standard short date. When `opts.hour`/`minute` is present a date-only
 * key (`YYYY-MM-DD`) still renders at local midnight; an ISO timestamp keeps its
 * exact instant.
 *
 * Returns the `placeholder` (default `—`) for a null/empty/unparseable value.
 * Read it inside a `$derived` / template so a locale switch re-renders.
 */
export function formatDate(
	value: string | null | undefined,
	placeholder = '—',
	opts: Intl.DateTimeFormatOptions = DEFAULT_DATE_OPTS
): string {
	if (!value) return placeholder;
	const d = parseLocalDate(value);
	if (!d) return placeholder;
	// `toLocaleDateString` ignores time parts; switch to `toLocaleString` when a
	// caller asks for hour/minute/second (e.g. a "Jun 20, 3:45 PM" datetime cell).
	const locale = getActiveFormatLocale();
	return opts.hour || opts.minute || opts.second
		? d.toLocaleString(locale, opts)
		: d.toLocaleDateString(locale, opts);
}

/**
 * Locale-aware short label for a reporting-period key — `YYYY-MM` (month) or
 * `YYYY-MM-DD` (day/week). Month keys render month + 2-digit year; day/week
 * keys add the day-of-month. Drives its locale off the active i18n locale,
 * the same way {@link formatDate} does. Returns the raw key unchanged if it
 * isn't a parseable date. The output is the same on every CLDR version: no
 * year-elision apostrophe ("Jun 26", never "Jun ’26").
 */
export function formatPeriod(p: string): string {
	// month keys are YYYY-MM; day/week keys are YYYY-MM-DD.
	const isMonthKey = p.length === 7;
	const d = parseLocalDate(p);
	if (!d) return p;
	// Assembled from parts rather than `toLocaleDateString`, so the label does
	// not change with the runtime's CLDR version. CLDR 49 (ICU 79: Node 26,
	// and browsers as they update) gave English a year-elision apostrophe
	// ("Jun ’26", "Jun 20, ’26") where every earlier CLDR, and CI's Node 24,
	// says "Jun 26". The apostrophe is dropped from the literal before the
	// year; CLDR still decides order, month names and every other separator.
	// docs/decisions.md §531.
	const parts = new Intl.DateTimeFormat(getActiveFormatLocale(), {
		month: 'short',
		day: isMonthKey ? undefined : 'numeric',
		year: '2-digit'
	}).formatToParts(d);
	return parts
		.map((part, i) =>
			part.type === 'literal' && parts[i + 1]?.type === 'year'
				? part.value.replace(YEAR_ELISION, '')
				: part.value
		)
		.join('');
}

/** A year-elision mark ("’26") a newer CLDR puts before a two-digit year. */
const YEAR_ELISION = /[\u2019']/g;

/**
 * A `YYYY-MM` month key as a reader says it, with the full year: "Nov 2026"
 * (`short`) or "November 2026" (`long`). Use it wherever a month stands alone
 * as a label, e.g. a billing period. {@link formatPeriod}'s two-digit year is
 * for dense chart axes; on its own "Nov 26" reads as the 26th of November.
 * Returns the raw key unchanged if it isn't a parseable month.
 */
export function formatMonth(key: string, style: 'short' | 'long' = 'short'): string {
	if (!/^\d{4}-\d{2}$/.test(key)) return key;
	const d = parseLocalDate(key);
	if (!d) return key;
	return d.toLocaleDateString(getActiveFormatLocale(), { month: style, year: 'numeric' });
}

/**
 * A reporting-period key spelled out for a sentence or an accessible name: a
 * `YYYY-MM` month key as {@link formatMonth} ("Jun 2026"), a `YYYY-MM-DD`
 * day/week key as {@link formatDate} ("Jun 20, 2026"). The rule: a dense chart
 * axis takes {@link formatPeriod}; prose, an `aria-label` or an SVG `<title>`
 * takes this, because "Jun 26" in a sentence reads as the 26th of June.
 * Returns the raw key unchanged if it isn't a parseable date.
 */
export function formatPeriodLong(p: string): string {
	if (/^\d{4}-\d{2}$/.test(p)) return formatMonth(p);
	return formatDate(p, p);
}

/**
 * The invoice's own due date when a payment schedule moved it — i.e. when the
 * EFFECTIVE due date a list shows (`due_date`: the current schedule's, else the
 * invoice's own, the date `is_overdue` and the overdue count read) differs from
 * the invoice's own (`own_due_date`). `null` when there is nothing to say:
 * either date missing, or the two the same. Compared on the `YYYY-MM-DD` key,
 * so a timestamp suffix never reads as a reschedule. Decisions §565.
 */
export function rescheduledFrom(
	effective: string | null | undefined,
	own: string | null | undefined
): string | null {
	if (!effective || !own) return null;
	return effective.slice(0, 10) === own.slice(0, 10) ? null : own;
}
