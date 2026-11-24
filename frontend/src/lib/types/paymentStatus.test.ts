import { describe, it, expect } from 'vitest';
import {
	CARD_STATUSES,
	CARD_STATUS_LABEL_KEYS,
	CARD_STATUS_TONES,
	PAYMENT_RUN_STATUSES,
	PAYMENT_STATUSES,
	PAYMENT_STATUS_LABEL_KEYS,
	RUN_STATUS_LABEL_KEYS,
	RUN_STATUS_TONES,
	paymentStatusLabelKey,
	cardStatusLabelKey,
	cardStatusTone,
	runStatusLabelKey,
	type PaymentStatus
} from './payment';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import { messages as de } from '#lib/i18n/locales/de.ts';
import { messages as es } from '#lib/i18n/locales/es.ts';
import { messages as fr } from '#lib/i18n/locales/fr.ts';
import { messages as ja } from '#lib/i18n/locales/ja.ts';
import { messages as ptBR } from '#lib/i18n/locales/pt-BR.ts';

/**
 * Drift guard: the frontend payment-status vocabulary must cover every status
 * the backend can persist on `payments.status`.
 *
 * A status missing here is not a cosmetic gap — the History row renders the
 * label behind `PAYMENT_STATUS_LABEL_KEYS[p.status]`, so an unlisted status
 * renders a BLANK badge, gets no filter chip, and becomes invisible in the
 * UI. That is exactly
 * what happened to `pending_compliance`: the sanctions/KYC gate parks a payment
 * there, and the payment then had no chip, no label, and no way forward.
 *
 * Sources (backend):
 *   - `services/payment_adapters/base.py::PaymentStatus` — pending, submitted,
 *     processing, completed, failed, cancelled (adapter + webhook vocabulary).
 *   - `api/payments.py` sets two more directly: `voided` (POST .../void) and
 *     `pending_compliance` (the compliance hold).
 */
const BACKEND_PAYMENT_STATUSES = [
	'pending',
	'pending_compliance',
	'submitted',
	'processing',
	'completed',
	'failed',
	'cancelled',
	'voided'
] as const;

describe('PAYMENT_STATUSES', () => {
	it('covers every status the backend can persist', () => {
		for (const status of BACKEND_PAYMENT_STATUSES) {
			expect(
				PAYMENT_STATUSES as readonly string[],
				`${status} is persisted by the backend but missing from PAYMENT_STATUSES`
			).toContain(status);
		}
	});

	it('offers no status the backend never persists', () => {
		const backend = new Set<string>(BACKEND_PAYMENT_STATUSES);
		for (const status of PAYMENT_STATUSES) {
			expect(backend.has(status), `${status} is not a backend payment status`).toBe(true);
		}
	});

	it('gives every status a label KEY that exists in the catalogue', () => {
		// `Record<PaymentStatus, MessageKey>` already fails `pnpm check` when a
		// status is added with no key — but a key naming nothing in the
		// catalogue typechecks fine and renders the raw key string in the badge
		// (`m()` falls back key → raw). This is the runtime half.
		for (const status of PAYMENT_STATUSES) {
			const key = PAYMENT_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(EN_ALL), `${status} → "${key}" is not in the catalogue`).toContain(key);
			expect(EN_ALL[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of PAYMENT_STATUSES) {
			expect(EN_ALL[PAYMENT_STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('paymentStatusLabelKey resolves a known status and returns null otherwise', () => {
		for (const status of PAYMENT_STATUSES) {
			expect(paymentStatusLabelKey(status)).toBe(PAYMENT_STATUS_LABEL_KEYS[status]);
		}
		// The caller renders the raw value — visible and searchable — rather
		// than a blank badge, so a status the backend adds first degrades
		// gracefully until this map catches up.
		expect(paymentStatusLabelKey('some_future_status')).toBeNull();
	});

	it('includes the compliance-hold parking state', () => {
		// Called out on its own: this is the status whose absence made a held
		// payment a dead end in the UI, with no chip and no release/dismiss path.
		const held: PaymentStatus = 'pending_compliance';
		expect(PAYMENT_STATUSES).toContain(held);
		expect(EN_ALL[PAYMENT_STATUS_LABEL_KEYS[held]]).toBe('Compliance Hold');
	});
});

/**
 * Drift guard: the frontend payment-RUN vocabulary must cover every status
 * `services/payment_runs.py` can report on `PaymentRun.status`.
 *
 * The run status is DERIVED on read (`derive_run_status` — the three
 * `CLAIM_RUN_STATUSES` pass through, the rest come from
 * `PaymentRunRollup.run_status`), so a rung this build has never seen arrives
 * as ordinary text. Both surfaces that badge it — the `/payments` Runs table
 * and `RunDetailModal`'s header — read the label through
 * `RUN_STATUS_LABEL_KEYS`, so a missing entry renders a raw enum value where
 * every neighbouring cell is translated.
 *
 * Sources (backend `services/payment_runs.py`):
 *   - `CLAIM_RUN_STATUSES` — draft, exported (the NACHA bank-file claim,
 *     issue #517), executing, cancelled.
 *   - `PaymentRunRollup.run_status` — failed, partial, submitted, draft,
 *     executing, completed.
 */
describe('RUN_STATUS_LABEL_KEYS', () => {
	// Mirrors CLAIM_RUN_STATUSES plus every value PaymentRunRollup.run_status
	// can return. Extend BOTH this list and the maps when the backend adds a
	// rung — this list is the assertion, not a restatement of the map.
	const BACKEND_RUN_STATUSES = [
		'draft',
		'exported',
		'executing',
		'cancelled',
		'submitted',
		'completed',
		'partial',
		'failed'
	];

	it('covers every run status the backend can report', () => {
		for (const status of BACKEND_RUN_STATUSES) {
			expect(
				PAYMENT_RUN_STATUSES as string[],
				`${status} is missing from PAYMENT_RUN_STATUSES`
			).toContain(status);
		}
	});

	it('names a real, non-empty catalogue key for every run status', () => {
		for (const status of PAYMENT_RUN_STATUSES) {
			const key = RUN_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(EN_ALL), `${status} -> "${key}" is not in the catalogue`).toContain(key);
			expect(EN_ALL[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of PAYMENT_RUN_STATUSES) {
			expect(EN_ALL[RUN_STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('labels every run status the tone map tints', () => {
		// The two are read one after the other on the same pill — a status with a
		// tone but no label is a coloured chip showing a raw enum value.
		for (const status of Object.keys(RUN_STATUS_TONES)) {
			expect(runStatusLabelKey(status), `${status} has a tone but no label`).toBeTruthy();
		}
	});

	it('runStatusLabelKey resolves a known status and returns null otherwise', () => {
		for (const status of PAYMENT_RUN_STATUSES) {
			expect(runStatusLabelKey(status)).toBe(RUN_STATUS_LABEL_KEYS[status]);
		}
		// `PaymentRun.status` is a bare string on the wire; the caller renders the
		// raw value rather than a blank badge for a rung this build does not know.
		expect(runStatusLabelKey('some_future_rollup')).toBeNull();
	});
});

/**
 * Drift guard: the virtual-card vocabulary must cover every status the backend
 * can write to `virtual_cards.status`, and every one must reach the cards
 * table's badge as a translated label.
 *
 * The badge printed `card.status` raw in every locale until round 36. Source
 * (backend): `services/card_adapters/base.py::CardStatus` — issuance writes
 * `created`, the provider webhooks `charged` / `completed`, cancel
 * `cancelled`, and the adapters' status sync maps the provider's own states
 * onto the rest.
 */
describe('CARD_STATUS_LABEL_KEYS', () => {
	// Mirrors `CardStatus`. Extend BOTH this list and the maps when the backend
	// adds a member — this list is the assertion, not a restatement of the map.
	const BACKEND_CARD_STATUSES = [
		'created',
		'sent',
		'active',
		'charged',
		'completed',
		'expired',
		'cancelled',
		'declined'
	];

	it('covers exactly the statuses the backend can write', () => {
		expect([...CARD_STATUSES].sort()).toEqual([...BACKEND_CARD_STATUSES].sort());
	});

	it('names a real, non-empty key for every status in every locale', () => {
		// The non-English catalogues are `satisfies Messages`, so a missing key
		// is already a compile error; this catches an empty or untranslated one.
		const locales = { de, es, fr, ja, 'pt-BR': ptBR } as Record<string, Record<string, string>>;
		for (const status of CARD_STATUSES) {
			const key = CARD_STATUS_LABEL_KEYS[status];
			expect(Object.keys(EN_ALL), `${status} -> "${key}" is not in the catalogue`).toContain(key);
			expect(EN_ALL[key].trim().length).toBeGreaterThan(0);
			for (const [locale, catalogue] of Object.entries(locales)) {
				expect(catalogue[key]?.trim(), `${locale} has no label for card status ${status}`).toBeTruthy();
				expect(catalogue[key], `${locale} renders card status ${status} raw`).not.toBe(status);
			}
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of CARD_STATUSES) {
			expect(EN_ALL[CARD_STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('labels every status the tone map tints', () => {
		for (const status of Object.keys(CARD_STATUS_TONES)) {
			expect(cardStatusLabelKey(status), `${status} has a tone but no label`).toBeTruthy();
		}
	});

	it('falls an unknown status back to the raw value and a flat tone', () => {
		expect(cardStatusLabelKey('some_future_state')).toBeNull();
		expect(cardStatusTone('some_future_state')).toBe('neutral');
		expect(cardStatusTone('declined')).toBe('danger');
	});
});
