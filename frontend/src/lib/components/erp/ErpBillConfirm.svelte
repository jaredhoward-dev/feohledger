<script lang="ts">
	/**
	 * A Xero push refused `erp_bill_key_shared` (decisions §572): another
	 * invoice carries the same supplier + number, so the push would not take the
	 * bill it found. Here a person finds that bill (read-only) and, having
	 * checked in the ERP, confirms it is this invoice's: the server re-runs the
	 * lookup, refuses a bill another invoice holds, records the confirmation and
	 * retries the push, which adopts that bill (decisions §587).
	 *
	 * A thin wrapper over `ErpBillLookup` (shared with `ErpOrphanBill`), which
	 * owns the lookup, the live region, the verdict's refusal and the confirm
	 * dialog with its required reason: this names the endpoints and the words.
	 * Rendered only where the server's verdict offers it (`verdict`, the
	 * `erp_bill_confirm_refusal` on the invoice's review-actions); hosts key it
	 * on the invoice id.
	 */
	import { api } from '#lib/api.ts';
	import type { ActionRefusal } from '#lib/api/actionVerdicts.ts';
	import ErpBillLookup, {
		type ErpBillLookupAction,
		type ErpBillLookupLabels
	} from '#lib/components/erp/ErpBillLookup.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	type Props = {
		invoiceId: string;
		/** The server's verdict (`null` = may search and confirm). */
		verdict: ActionRefusal;
		/** Prefix for element ids and test ids, unique per host. */
		idPrefix?: string;
		/** Called after a confirmation, so the host re-reads the invoice. */
		onconfirmed?: () => void | Promise<void>;
	};

	let { invoiceId, verdict, idPrefix = 'erp-bill-confirm', onconfirmed }: Props = $props();

	const path = $derived(`/api/invoices/${encodeURIComponent(invoiceId)}`);

	const labels: ErpBillLookupLabels = $derived({
		title: m('invoices.modal.erpBillConfirm.title'),
		intro: m('invoices.modal.erpBillConfirm.intro'),
		find: m('invoices.modal.erpBillConfirm.find'),
		finding: m('invoices.modal.erpBillConfirm.finding'),
		none: m('invoices.modal.erpBillConfirm.none'),
		failed: m('invoices.modal.erpBillConfirm.failed'),
		statusLabel: m('invoices.modal.erpBillConfirm.statusLabel'),
		idLabel: m('invoices.modal.erpBillConfirm.idLabel'),
		found: (number) => m('invoices.modal.erpBillConfirm.found', { number })
	});

	const action: ErpBillLookupAction = $derived({
		tone: 'primary',
		openLabel: m('invoices.modal.erpBillConfirm.open'),
		confirmTitle: m('invoices.modal.erpBillConfirm.confirmTitle'),
		confirmBody: m('invoices.modal.erpBillConfirm.confirmBody'),
		reasonLabel: m('invoices.modal.erpBillConfirm.reason'),
		submit: m('invoices.modal.erpBillConfirm.confirm'),
		submitting: m('invoices.modal.erpBillConfirm.confirming'),
		failed: m('invoices.modal.erpBillConfirm.confirmFailed'),
		testIds: {
			open: 'open',
			form: 'form',
			reason: 'reason',
			error: 'confirm-error',
			submit: 'confirm'
		},
		commit: async (body, bill) => {
			await api.post(`${path}/confirm-erp-bill`, body);
			return {
				toast: m('invoices.modal.erpBillConfirm.done', {
					number: bill.erp_document_number ?? bill.erp_document_id
				})
			};
		}
	});
</script>

<ErpBillLookup
	lookupPath="{path}/erp-bill-confirmation"
	{verdict}
	{idPrefix}
	{labels}
	{action}
	ondone={onconfirmed}
/>
