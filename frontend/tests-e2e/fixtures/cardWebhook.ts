import { createHmac } from 'node:crypto';

/**
 * Card-webhook deliveries signed the way each provider really authenticates
 * them (decisions §555), so a spec exercises the same verification a real
 * delivery meets.
 *
 * - Lithic (https://docs.lithic.com/docs/events-api, checked 2026-11-25):
 *   Standard-Webhooks — `webhook-id` / `webhook-timestamp` /
 *   `webhook-signature`, the signature `v1,<base64 HMAC-SHA256>` over
 *   `"{id}.{timestamp}.{raw body}"`, keyed by the base64 part of a
 *   `whsec_`-prefixed secret; a timestamp more than five minutes off is
 *   refused.
 * - Nium (https://docs.nium.com/docs/developers/notifications-and-webhooks):
 *   no body signature — the static `x-partner-key` the tenant agreed with
 *   Nium, and the delivery id in `x-request-id`.
 */

/** A `whsec_` secret whose key is ``raw`` (any text). */
export function lithicSecret(raw: string): string {
	return `whsec_${Buffer.from(raw, 'utf8').toString('base64')}`;
}

export interface LithicSignOptions {
	/** The delivery id (`webhook-id`), which is also the dedupe key. */
	id: string;
	/** Unix seconds; defaults to now. */
	timestamp?: number;
}

/** The three Standard-Webhooks headers for ``rawBody`` under ``secret``. */
export function lithicHeaders(
	secret: string,
	rawBody: string,
	{ id, timestamp }: LithicSignOptions
): Record<string, string> {
	const ts = timestamp ?? Math.floor(Date.now() / 1000);
	const key = Buffer.from(secret.replace(/^whsec_/, ''), 'base64');
	const signature = createHmac('sha256', key).update(`${id}.${ts}.${rawBody}`).digest('base64');
	return {
		'Content-Type': 'application/json',
		'webhook-id': id,
		'webhook-timestamp': String(ts),
		'webhook-signature': `v1,${signature}`
	};
}

/** Nium's headers: the partner key and the delivery id. */
export function niumHeaders(partnerKey: string, requestId: string): Record<string, string> {
	return {
		'Content-Type': 'application/json',
		'x-request-id': requestId,
		'x-partner-key': partnerKey
	};
}
