<script lang="ts">
	/**
	 * The bill a failed ERP push may have left in the ERP (decisions §556):
	 * search the ERP for it (read-only), then void it or keep it. Shared by the
	 * invoice modal and the exception dialog, so both say and do the same.
	 *
	 * A thin wrapper over `ErpBillLookup`, which owns the lookup, the live
	 * region, the verdict's refusal and the confirm dialog: this names the
	 * endpoints, the words and the keep hint. Rendered only where the server's
	 * verdict offers it (`verdict`, the `erp_orphan_lookup_refusal` the
	 * invoice's review-actions and the exception row carry, decisions §560).
	 *
	 * The void stacks a confirm dialog of its own (a required reason) on the
	 * host's dialog, like the flag's void: it reaches into the customer's ERP
	 * and cannot be undone from here. Keeping the bill needs no action here:
	 * Retry ERP Send records the bill the push's own lookup finds.
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
		/** The server's verdict on the lookup (`null` = may search). */
		verdict: ActionRefusal;
		/** Prefix for element ids and test ids, unique per host. */
		idPrefix?: string;
		/** Where the invoice opens, for a host with no Retry ERP Send of its own
		 *  (the exception dialog): the keep hint links there instead of naming
		 *  a button the reader cannot see. */
		invoiceHref?: string | null;
		/** Called after a void, so the host re-reads the invoice / flag. */
		onvoided?: () => void | Promise<void>;
	};

	let { invoiceId, verdict, idPrefix = 'erp-orphan', invoiceHref = null, onvoided }: Props =
		$props();

	const path = $derived(`/api/invoices/${encodeURIComponent(invoiceId)}`);
	// The button's own label, so the hints name the control as each locale
	// labels it.
	const retry = $derived(m('invoices.modal.erp.retrySend'));

	const labels: ErpBillLookupLabels = $derived({
		title: m('invoices.modal.erpOrphan.title'),
		intro: m('invoices.modal.erpOrphan.intro'),
		find: m('invoices.modal.erpOrphan.find'),
		finding: m('invoices.modal.erpOrphan.finding'),
		none: m('invoices.modal.erpOrphan.none'),
		failed: m('invoices.modal.erpOrphan.failed'),
		statusLabel: m('invoices.modal.erpOrphan.statusLabel'),
		idLabel: m('invoices.modal.erpOrphan.idLabel'),
		found: (number) => m('invoices.modal.erpOrphan.found', { number })
	});

	const action: ErpBillLookupAction = $derived({
		tone: 'danger',
		openLabel: m('invoices.modal.erpOrphan.void'),
		confirmTitle: m('invoices.modal.erpOrphan.confirmTitle'),
		confirmBody: m('invoices.modal.erpOrphan.confirmBody'),
		reasonLabel: m('invoices.modal.erpOrphan.reason'),
		submit: m('invoices.modal.erpOrphan.confirm'),
		submitting: m('invoices.modal.erpOrphan.voiding'),
		failed: m('invoices.modal.erpOrphan.voidFailed'),
		testIds: {
			open: 'void-open',
			form: 'void-form',
			reason: 'void-reason',
			error: 'void-error',
			submit: 'void-confirm'
		},
		commit: async (body, bill) => {
			await api.post(`${path}/void-erp-orphan-bill`, body);
			const number = bill.erp_document_number ?? bill.erp_document_id;
			return {
				toast: m('invoices.modal.erpOrphan.done', { number }),
				note: m('invoices.modal.erpOrphan.voided', { number, retry })
			};
		}
	});
</script>

<ErpBillLookup
	lookupPath="{path}/erp-orphan-bill"
	{verdict}
	{idPrefix}
	{labels}
	{action}
	ondone={onvoided}
>
	{#snippet afterFound()}
		{#if invoiceHref}
			<p class="hint">
				{m('invoices.modal.erpOrphan.keepHintFromInvoice')}
				<a href={invoiceHref} data-testid="{idPrefix}-open-invoice"
					>{m('invoices.modal.erpOrphan.openInvoice')}</a
				>
			</p>
		{:else}
			<p class="hint">{m('invoices.modal.erpOrphan.keepHint', { retry })}</p>
		{/if}
	{/snippet}
</ErpBillLookup>
