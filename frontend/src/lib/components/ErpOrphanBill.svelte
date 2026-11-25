<script lang="ts">
	/**
	 * The bill a failed ERP push may have left in the ERP (decisions §556):
	 * search the ERP for it (read-only), then void it or keep it. Shared by the
	 * invoice modal and the exception dialog, so both say and do the same.
	 *
	 * The void stacks a confirm dialog of its own (a required reason) on the
	 * host's dialog, like the flag's void: it reaches into the customer's ERP
	 * and cannot be undone from here. Keeping the bill needs no action here:
	 * Retry ERP Send records the bill the push's own lookup finds.
	 */
	import { api } from '#lib/api.ts';
	import {
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
		/** Prefix for element ids and test ids, unique per host. */
		idPrefix?: string;
		/** Called after a void, so the host re-reads the invoice / flag. */
		onvoided?: () => void | Promise<void>;
	};

	let { invoiceId, idPrefix = 'erp-orphan', onvoided }: Props = $props();

	let lookup = $state<OrphanState>({ kind: 'idle' });
	let confirmOpen = $state(false);
	let reason = $state('');
	let voiding = $state(false);
	let voidError = $state('');

	async function find() {
		lookup = { kind: 'loading' };
		try {
			const res = await api.get<OrphanLookup>(
				`/api/invoices/${encodeURIComponent(invoiceId)}/erp-orphan-bill`
			);
			lookup = orphanStateFrom(res, m('invoices.modal.erpOrphan.failed'));
		} catch (err) {
			lookup = orphanStateFrom(err, m('invoices.modal.erpOrphan.failed'));
		}
	}

	function openConfirm() {
		reason = '';
		voidError = '';
		confirmOpen = true;
	}

	async function commitVoid() {
		if (lookup.kind !== 'found' || !reason.trim()) return;
		const bill = lookup.bill;
		voiding = true;
		voidError = '';
		try {
			await api.post(`/api/invoices/${encodeURIComponent(invoiceId)}/void-erp-orphan-bill`, {
				erp_document_id: bill.erp_document_id,
				reason: reason.trim()
			});
			toast(
				m('invoices.modal.erpOrphan.done', {
					number: bill.erp_document_number ?? bill.erp_document_id
				}),
				'success'
			);
			confirmOpen = false;
			lookup = { kind: 'idle' };
			await onvoided?.();
		} catch (err) {
			voidError = formatApiDetail(
				err instanceof Error ? err.message : undefined,
				m('invoices.modal.erpOrphan.voidFailed')
			);
		} finally {
			voiding = false;
		}
	}
</script>

<section class="erp-orphan" data-testid={idPrefix} aria-labelledby="{idPrefix}-title">
	<h3 id="{idPrefix}-title">{m('invoices.modal.erpOrphan.title')}</h3>
	<p class="hint">{m('invoices.modal.erpOrphan.intro')}</p>

	{#if lookup.kind === 'found'}
		<p class="found" data-testid="{idPrefix}-found">
			{m('invoices.modal.erpOrphan.found', {
				number: lookup.bill.erp_document_number ?? lookup.bill.erp_document_id,
				status: lookup.bill.erp_status
			})}
			{#if lookup.bill.amount !== null}
				<Money amount={lookup.bill.amount} currency={lookup.bill.currency} />
			{/if}
			<code>{lookup.bill.erp_document_id}</code>
		</p>
		<p class="hint">{m('invoices.modal.erpOrphan.keepHint')}</p>
	{:else if lookup.kind === 'none'}
		<p class="hint" data-testid="{idPrefix}-none">{m('invoices.modal.erpOrphan.none')}</p>
	{:else if lookup.kind === 'refused'}
		<RefusalNote id="{idPrefix}-refusal" testid="{idPrefix}-refusal" reason={lookup.message} />
	{:else if lookup.kind === 'error'}
		<p class="error" role="alert" data-testid="{idPrefix}-error">{lookup.message}</p>
	{/if}

	<div class="actions">
		<button
			type="button"
			class="btn-secondary"
			data-testid="{idPrefix}-find"
			disabled={lookup.kind === 'loading'}
			aria-describedby={lookup.kind === 'refused' ? `${idPrefix}-refusal` : undefined}
			onclick={find}
		>
			{lookup.kind === 'loading'
				? m('invoices.modal.erpOrphan.finding')
				: m('invoices.modal.erpOrphan.find')}
		</button>
		{#if lookup.kind === 'found'}
			<button
				type="button"
				class="btn-danger"
				data-testid="{idPrefix}-void-open"
				onclick={openConfirm}
			>
				{m('invoices.modal.erpOrphan.void')}
			</button>
		{/if}
	</div>
</section>

<Modal
	open={confirmOpen}
	ariaLabel={m('invoices.modal.erpOrphan.confirmTitle')}
	width="sm"
	onclose={() => (confirmOpen = false)}
>
	<h2>{m('invoices.modal.erpOrphan.confirmTitle')}</h2>
	{#if lookup.kind === 'found'}
		<p class="hint">
			<code>{lookup.bill.erp_document_number ?? lookup.bill.erp_document_id}</code>
			{#if lookup.bill.amount !== null}
				· <Money amount={lookup.bill.amount} currency={lookup.bill.currency} />
			{/if}
		</p>
	{/if}
	<p class="hint">{m('invoices.modal.erpOrphan.confirmBody')}</p>
	<form
		data-testid="{idPrefix}-void-form"
		onsubmit={(e) => {
			e.preventDefault();
			commitVoid();
		}}
	>
		<label>
			<span>{m('invoices.modal.erpOrphan.reason')}</span>
			<input
				type="text"
				bind:value={reason}
				maxlength="500"
				required
				data-autofocus
				data-testid="{idPrefix}-void-reason"
			/>
		</label>
		{#if voidError}
			<p class="error" role="alert" data-testid="{idPrefix}-void-error">{voidError}</p>
		{/if}
		<div class="actions">
			<button type="button" class="btn-cancel" onclick={() => (confirmOpen = false)}>
				{m('common.cancel')}
			</button>
			<button
				type="submit"
				class="btn-danger"
				data-testid="{idPrefix}-void-confirm"
				disabled={voiding || !reason.trim()}
			>
				{voiding ? m('invoices.modal.erpOrphan.voiding') : m('invoices.modal.erpOrphan.confirm')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.erp-orphan {
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
