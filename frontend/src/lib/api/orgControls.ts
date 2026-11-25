/**
 * The two single-key organization controls (admin only) —
 * `backend/app/api/organization_controls.py`, decisions §502. Each GET reads
 * the effective value; each PUT is audited and answers 503 with nothing saved
 * when the audit row cannot be written.
 */
import { api } from '#lib/api.ts';
import type {
	AutonomyLevel,
	AutonomySetting,
	LineTotalCheckSetting
} from '#lib/types/orgControls.ts';

const LINE_TOTAL = '/api/organization/line-total-check';
const AUTONOMY = '/api/organization/exception-agents/autonomy';

export function getLineTotalCheck(): Promise<LineTotalCheckSetting> {
	return api.get<LineTotalCheckSetting>(LINE_TOTAL);
}

export function saveLineTotalCheck(enabled: boolean): Promise<LineTotalCheckSetting> {
	return api.put<LineTotalCheckSetting>(LINE_TOTAL, { enabled });
}

export function getAgentAutonomy(): Promise<AutonomySetting> {
	return api.get<AutonomySetting>(AUTONOMY);
}

export function saveAgentAutonomy(level: AutonomyLevel): Promise<AutonomySetting> {
	return api.put<AutonomySetting>(AUTONOMY, { autonomy_level: level });
}
