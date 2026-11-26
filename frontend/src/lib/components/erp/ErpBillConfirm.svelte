<script lang="ts">
	/**
	 * A Xero push refused `erp_bill_key_shared` (decisions §572): another
	 * invoice carries the same supplier + number, so the push would not take the
	 * bill it found. Here a person finds that bill (read-only) and, having
	 * checked in the ERP, confirms it is this invoice's: the server re-runs the
	 * lookup, refuses a bill another invoice holds, records the confirmation and
	 * retries the push, which adopts that bill (decisions §587).
	 *
	 * Rendered only where the server's verdict offers it (`verdict`, the
	 * `erp_bill_confirm_refusal` on the invoice's review-actions): the host
	 * checks `isOffered`, this states a refusal beside a disabled Find. Hosts
	 * key it on the invoice id. The confirmation stacks a dialog with a required
	 * reason, like the orphan bill's void (`ErpOrphanBill.svelte`), whose lookup
	 * state and answer shape it shares.
	 */
	import { tick } from 'svelte';
	import { api } from '#lib/api.ts';
	import { isAllowed, refusalText, type ActionRefusal } from '#lib/api/actionVerdicts.ts';
	import {
		confirmBillBody,
		orphanStateFrom,
		type OrphanLookup,
		type OrphanState
	} from '#lib/api/erpOrphanBill.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import RefusalNote from '#lib/components/ui/RefusalNote.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatApiDetail } from '#lib/utils/apiError.ts';

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

	let lookup = $state<OrphanState>({ kind: 'idle' });
	let confirmOpen = $state(false);
	let reason = $state('');
	let confirming = $state(false);
	let confirmError = $state('');
	let findButton = $state<HTMLButtonElement | null>(null);
	const refusal = $derived(verdict ? refusalText(verdict, m) : '');
	const body = $derived(confirmBillBody(lookup, reason));

	async function find() {
		lookup = { kind: 'loading' };
		try {
			const res = await api.get<OrphanLookup>(
				`/api/invoices/${encodeURIComponent(invoiceId)}/erp-bill-confirmation`
			);
			lookup = orphanStateFrom(res, m('invoices.modal.erpBillConfirm.failed'));
		} catch (err) {
			lookup = orphanStateFrom(err, m('invoices.modal.erpBillConfirm.failed'));
		}
	}

	function openConfirm() {
		reason = '';
		confirmError = '';
		confirmOpen = true;
	}

	async function commit() {
		if (lookup.kind !== 'found' || body === null) return;
		const bill = lookup.bill;
		confirming = true;
		confirmError = '';
		try {
			await api.post(`/api/invoices/${encodeURIComponent(invoiceId)}/confirm-erp-bill`, body);
			toast(
				m('invoices.modal.erpBillConfirm.done', {
					number: bill.erp_document_number ?? bill.erp_document_id
				}),
				'success'
			);
			confirmOpen = false;
			lookup = { kind: 'idle' };
			// The trigger went with the found bill: focus Find in the host
			// dialog rather than the page behind it (WCAG 2.4.3).
			await tick();
			findButton?.focus();
			await onconfirmed?.();
		} catch (err) {
			confirmError = formatApiDetail(
				err instanceof Error ? err.message : undefined,
				m('invoices.modal.erpBillConfirm.confirmFailed')
			);
		} finally {
			confirming = false;
		}
	}
</script>

<section class="erp-confirm" data-testid={idPrefix} aria-labelledby="{idPrefix}-title">
	<h3 id="{idPrefix}-title">{m('invoices.modal.erpBillConfirm.title')}</h3>
	<p class="hint">{m('invoices.modal.erpBillConfirm.intro')}</p>

	<!-- One live region for the result chain, mounted from first paint. -->
	<div class="result" role="status" data-testid="{idPrefix}-status">
		{#if lookup.kind === 'loading'}
			<p class="hint">{m('invoices.modal.erpBillConfirm.finding')}</p>
		{:else if lookup.kind === 'found'}
			<p class="found" data-testid="{idPrefix}-found">
				{m('invoices.modal.erpBillConfirm.found', {
					number: lookup.bill.erp_document_number ?? lookup.bill.erp_document_id
				})}
				{#if lookup.bill.amount !== null}
					<Money amount={lookup.bill.amount} currency={lookup.bill.currency} />
				{/if}
			</p>
			<dl class="facts">
				<dt>{m('invoices.modal.erpOrphan.statusLabel')}</dt>
				<dd><code data-testid="{idPrefix}-erp-status">{lookup.bill.erp_status}</code></dd>
				{#if lookup.bill.erp_document_number}
					<dt>{m('invoices.modal.erpOrphan.idLabel')}</dt>
					<dd><code data-testid="{idPrefix}-erp-id">{lookup.bill.erp_document_id}</code></dd>
				{/if}
			</dl>
		{:else if lookup.kind === 'none'}
			<p class="hint" data-testid="{idPrefix}-none">{m('invoices.modal.erpBillConfirm.none')}</p>
		{:else if lookup.kind === 'refused'}
			<RefusalNote id="{idPrefix}-refusal" testid="{idPrefix}-refusal" reason={lookup.message} />
		{/if}
	</div>
	{#if lookup.kind === 'error'}
		<p class="error" role="alert" data-testid="{idPrefix}-error">{lookup.message}</p>
	{/if}
	{#if refusal}
		<RefusalNote id="{idPrefix}-verdict" testid="{idPrefix}-verdict" reason={refusal} />
	{/if}

	<div class="actions">
		<button
			type="button"
			class="btn-secondary"
			data-testid="{idPrefix}-find"
			bind:this={findButton}
			disabled={lookup.kind === 'loading' || !isAllowed(verdict)}
			aria-describedby={refusal
				? `${idPrefix}-verdict`
				: lookup.kind === 'refused'
					? `${idPrefix}-refusal`
					: undefined}
			onclick={find}
		>
			{lookup.kind === 'loading'
				? m('invoices.modal.erpBillConfirm.finding')
				: m('invoices.modal.erpBillConfirm.find')}
		</button>
		{#if lookup.kind === 'found'}
			<button
				type="button"
				class="btn-primary"
				data-testid="{idPrefix}-open"
				onclick={openConfirm}
			>
				{m('invoices.modal.erpBillConfirm.open')}
			</button>
		{/if}
	</div>
</section>

<Modal
	open={confirmOpen}
	ariaLabel={m('invoices.modal.erpBillConfirm.confirmTitle')}
	width="sm"
	onclose={() => (confirmOpen = false)}
>
	<h2>{m('invoices.modal.erpBillConfirm.confirmTitle')}</h2>
	{#if lookup.kind === 'found'}
		<p class="hint">
			<code>{lookup.bill.erp_document_number ?? lookup.bill.erp_document_id}</code>
			{#if lookup.bill.amount !== null}
				· <Money amount={lookup.bill.amount} currency={lookup.bill.currency} />
			{/if}
		</p>
	{/if}
	<p class="hint">{m('invoices.modal.erpBillConfirm.confirmBody')}</p>
	<form
		data-testid="{idPrefix}-form"
		onsubmit={(e) => {
			e.preventDefault();
			commit();
		}}
	>
		<label>
			<span>{m('invoices.modal.erpBillConfirm.reason')}</span>
			<input
				type="text"
				bind:value={reason}
				maxlength="500"
				required
				data-autofocus
				data-testid="{idPrefix}-reason"
			/>
		</label>
		{#if confirmError}
			<p class="error" role="alert" data-testid="{idPrefix}-confirm-error">{confirmError}</p>
		{/if}
		<div class="actions">
			<button type="button" class="btn-cancel" onclick={() => (confirmOpen = false)}>
				{m('common.cancel')}
			</button>
			<button
				type="submit"
				class="btn-primary"
				data-testid="{idPrefix}-confirm"
				disabled={confirming || body === null}
			>
				{confirming
					? m('invoices.modal.erpBillConfirm.confirming')
					: m('invoices.modal.erpBillConfirm.confirm')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.erp-confirm {
		margin-top: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	h3 {
		font-size: 0.875rem;
		margin: 0;
	}
	.hint {
		color: var(--text-muted);
		font-size: 0.8125rem;
		margin: 0;
	}
	.found {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
		align-items: baseline;
	}
	.facts {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.125rem 0.5rem;
		margin: 0;
		font-size: 0.8125rem;
	}
	.facts dt {
		color: var(--text-muted);
	}
	.facts dd {
		margin: 0;
		overflow-wrap: anywhere;
	}
	.result {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}
	.result:empty {
		display: none;
	}
	.error {
		color: var(--danger);
		margin: 0;
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		justify-content: flex-end;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
</style>
