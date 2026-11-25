// Two single-key organization controls with their own audited writers
// (`backend/app/api/organization_controls.py`, decisions §502): the
// line-total reconciliation check and the exception-agent autonomy level.

import type { MessageKey } from '#lib/i18n/messages.ts';

/** `settings.fraud_rules` key owned by `PUT /api/organization/line-total-check`. */
export const LINE_TOTAL_RULE = 'line_total_mismatch_enabled';

/** Mirrors `organization_controls.AutonomyLevel`, safest first. */
export const AUTONOMY_LEVELS = ['conservative', 'balanced', 'aggressive'] as const;
export type AutonomyLevel = (typeof AUTONOMY_LEVELS)[number];

export interface LineTotalCheckSetting {
	enabled: boolean;
}

export interface AutonomySetting {
	autonomy_level: AutonomyLevel;
}

/**
 * The fraud-rules block as the generic `PATCH /api/organization` should carry
 * it: without the key the line-total panel owns. The Fraud Detection form is
 * filled from the defaults, so its copy of that key can be stale after the
 * line-total panel saves; omitting it lets the server carry the stored value
 * instead of refusing a mismatch.
 */
export function withoutOwnedFraudKeys<T extends object>(rules: T): T {
	const copy = { ...rules } as Record<string, unknown>;
	delete copy[LINE_TOTAL_RULE];
	return copy as T;
}

const LABEL_KEYS: Record<AutonomyLevel, MessageKey> = {
	conservative: 'org.agentAutonomy.conservative',
	balanced: 'org.agentAutonomy.balanced',
	aggressive: 'org.agentAutonomy.aggressive'
};

const HINT_KEYS: Record<AutonomyLevel, MessageKey> = {
	conservative: 'org.agentAutonomy.conservativeHint',
	balanced: 'org.agentAutonomy.balancedHint',
	aggressive: 'org.agentAutonomy.aggressiveHint'
};

export function autonomyLabelKey(level: AutonomyLevel): MessageKey {
	return LABEL_KEYS[level];
}

export function autonomyHintKey(level: AutonomyLevel): MessageKey {
	return HINT_KEYS[level];
}
