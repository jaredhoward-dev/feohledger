// Labels for the invoice modal's Activity timeline.
//
// The timeline lists every audit row correlated to the invoice — its own
// `invoice.*` events, the exception, chat and ERP events keyed to it — so its
// vocabulary is the whole backend audit vocabulary, not a hand-picked subset.
// A short list of invoice-voice overrides below reads better in the timeline
// than the /audit console's labels ("Uploaded" beside an invoice, where the
// console needs "Invoice uploaded"); every other token falls through to
// `auditActionLabel`, whose `audit.action.*` catalogue the roster guard
// (`auditActions.roster.test.ts`) holds complete against the backend source.
// The modal loads that catalogue slice itself (`ensureSlices('audit')`).
//
// The rows are stored as soon as their fetch returns (the ERP status panel
// reads them and needs no labels), but the Activity list draws them only once
// that slice has landed too (`activityTimelineView`), so they render labelled
// the first time (§529, §534).
//
// Before decisions §522 the modal's map was the only source, so a token it
// lacked (`invoice.erp_failed`, `invoice.erp_retried`, …) rendered raw.

import type { MessageKey } from '#lib/i18n/messages.ts';
import { hasMessage } from '#lib/i18n/store.svelte.ts';
import { auditActionLabel } from './auditActions';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/** Invoice-voice labels that take precedence over the /audit console's. */
export const INVOICE_TIMELINE_LABEL_KEYS: Record<string, MessageKey> = {
	'invoice.uploaded': 'invoices.modal.action.uploaded',
	'invoice.submitted_for_review': 'invoices.modal.action.submittedForReview',
	'invoice.approved': 'invoices.modal.action.approved',
	'invoice.rejected': 'invoices.modal.action.rejected',
	'invoice.resubmitted': 'invoices.modal.action.resubmitted',
	'invoice.approval_recalled': 'invoices.modal.action.approvalRecalled',
	'invoice.assigned_for_review': 'invoices.modal.action.assignedForReview',
	'invoice.erp_submitted': 'invoices.modal.action.erpSubmitted',
	'invoice.erp_auto_send_refused': 'invoices.modal.action.erpAutoSendRefused',
	'invoice.extraction_dispatched': 'invoices.modal.action.extractionStarted',
	'invoice.extraction_reset': 'invoices.modal.action.extractionReset',
	'invoice.extraction_triggered': 'invoices.modal.action.extractionTriggered',
	'invoice.extraction_completed': 'invoices.modal.action.extractionCompleted',
	'invoice.extraction_failed': 'invoices.modal.action.extractionFailed',
	'invoice.completed': 'invoices.modal.action.markedComplete',
	'invoice.edited': 'invoices.modal.action.editedFields',
	'invoice.file_attached': 'invoices.modal.action.fileAttached',
	'invoice.file_replaced': 'invoices.modal.action.fileReplaced',
	'invoice.file_deleted': 'invoices.modal.action.fileDeleted',
	'audit.viewed': 'invoices.modal.action.auditViewed',
	'audit.exported': 'invoices.modal.action.auditExported',
	// Exception-queue events. They are correlation-keyed to the invoice
	// (backend services/exception_lifecycle), so they arrive in this same
	// timeline — a raised duplicate / fraud flag is why the invoice stalled,
	// and clearing it is what let it move.
	'exception.raised': 'invoices.modal.action.exceptionRaised',
	'exception.resolved': 'invoices.modal.action.exceptionResolved',
	'exception.escalated': 'invoices.modal.action.exceptionEscalated',
	'exception.dismissed': 'invoices.modal.action.exceptionDismissed',
	'exception.assigned': 'invoices.modal.action.exceptionAssigned',
	chat_message_posted: 'invoices.modal.action.chatPosted',
	chat_thread_resolved: 'invoices.modal.action.chatResolved',
	chat_thread_reopened: 'invoices.modal.action.chatReopened'
};

/** A translated timeline label for `action`: the invoice-voice override, else
 *  the /audit console's label, else the token itself (a retired action on an
 *  old row, or one newer than this build). */
export function invoiceTimelineActionLabel(
	action: string,
	t: Translate,
	has: (key: string) => boolean = hasMessage
): string {
	return Object.hasOwn(INVOICE_TIMELINE_LABEL_KEYS, action)
		? t(INVOICE_TIMELINE_LABEL_KEYS[action])
		: auditActionLabel(action, t, has);
}

/** What the Activity list shows: a loading status, the load failure, its
 *  empty state, or the rows. */
export type ActivityTimelineView = 'loading' | 'errored' | 'empty' | 'rows';

/**
 * Which of the four async states the Activity list is in (decisions §534).
 *
 * Nothing but `loading` until the `audit` label slice has landed, however the
 * fetch went: that is the guarantee that no row is ever drawn with its raw
 * token and relabelled a moment later (§529). After that, rows already held
 * stay up through a refetch (no flash back to loading after every action), a
 * list with none waits for its first fetch, and only a settled fetch decides
 * between the load failure and the genuinely empty state.
 */
export function activityTimelineView(state: {
	labelsReady: boolean;
	fetched: boolean;
	errored: boolean;
	rowCount: number;
}): ActivityTimelineView {
	if (!state.labelsReady) return 'loading';
	if (state.rowCount > 0) return 'rows';
	if (!state.fetched) return 'loading';
	return state.errored ? 'errored' : 'empty';
}
