<script lang="ts" module>
	import type { OrphanBill } from '#lib/api/erpOrphanBill.ts';

	/** Every string the panel says, already localized by the wrapper. */
	export type ErpBillLookupLabels = {
		title: string;
		intro: string;
		find: string;
		finding: string;
		none: string;
		/** The fallback when a lookup fails with no detail. */
		failed: string;
		statusLabel: string;
		idLabel: string;
		found: (number: string) => string;
	};

	/** What a person does with the bill found: one stacked confirm dialog with
	 *  a required reason, then `commit`. */
	export type ErpBillLookupAction = {
		tone: 'danger' | 'primary';
		openLabel: string;
		confirmTitle: string;
		confirmBody: string;
		reasonLabel: string;
		submit: string;
		submitting: string;
		/** The fallback when the commit fails with no detail. */
		failed: string;
		/** Test-id suffixes after `{idPrefix}-`, per wrapper, so each keeps the
		 *  selectors its specs already use. */
		testIds: { open: string; form: string; reason: string; error: string; submit: string };
		/** POST the action. Resolves to the success toast and, optionally, a line
		 *  the live region keeps saying until the host remounts the panel. */
		commit: (
			body: { erp_document_id: string; reason: string },
			bill: OrphanBill
		) => Promise<{ toast: string; note?: string }>;
	};
</script>

<script lang="ts">
	/**
	 * Find a bill in the customer's ERP (read-only), then act on it through a
	 * stacked confirm dialog with a required reason. The shared body of
	 * `ErpOrphanBill` (void the bill a failed push left, decisions §556) and
	 * `ErpBillConfirm` (confirm the bill on a shared Xero key as this invoice's,
	 * §587): the lookup state machine, the one live region, the verdict's
	 * `RefusalNote` on a disabled Find, the confirm `ui/Modal`, and focus after
	 * the commit. The wrappers own the endpoints, the words and what follows.
	 *
	 * Rendered only where the server's verdict offers it: the host checks
	 * `isOffered`, this states a refusal beside a disabled Find. Hosts key it on
	 * the invoice id, so one invoice's bill never shows on another's.
	 */
	import { tick, type Snippet } from 'svelte';
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
		/** `GET` path of the read-only lookup. */
		lookupPath: string;
		/** The server's verdict (`null` = may search). */
		verdict: ActionRefusal;
		/** Prefix for element ids and test ids, unique per host. */
		idPrefix: string;
		labels: ErpBillLookupLabels;
		action: ErpBillLookupAction;
		/** Rendered in the live region under a found bill's facts. */
		afterFound?: Snippet;
		/** Called after a commit, so the host re-reads (or closes). */
		ondone?: () => void | Promise<void>;
	};

	let { lookupPath, verdict, idPrefix, labels, action, afterFound, ondone }: Props = $props();

	let lookup = $state<OrphanState>({ kind: 'idle' });
	let confirmOpen = $state(false);
	let reason = $state('');
	let submitting = $state(false);
	let submitError = $state('');
	/** What the commit said, kept in the live region until the host remounts. */
	let note = $state<string | null>(null);
	let findButton = $state<HTMLButtonElement | null>(null);
	const refusal = $derived(verdict ? refusalText(verdict, m) : '');
	const body = $derived(confirmBillBody(lookup, reason));
	const billNumber = (bill: OrphanBill) => bill.erp_document_number ?? bill.erp_document_id;

	async function find() {
		lookup = { kind: 'loading' };
		note = null;
		try {
			const res = await api.get<OrphanLookup>(lookupPath);
			lookup = orphanStateFrom(res, labels.failed);
		} catch (err) {
			lookup = orphanStateFrom(err, labels.failed);
		}
	}

	function openConfirm() {
		reason = '';
		submitError = '';
		confirmOpen = true;
	}

	async function commit() {
		if (lookup.kind !== 'found' || body === null) return;
		const bill = lookup.bill;
		submitting = true;
		submitError = '';
		try {
			const said = await action.commit(body, bill);
			toast(said.toast, 'success');
			confirmOpen = false;
			lookup = { kind: 'idle' };
			note = said.note ?? null;
			// The confirm's trigger went with the found bill, so focus lands on
			// Find, in the host dialog, rather than falling back to the page
			// behind it (WCAG 2.4.3).
			await tick();
			findButton?.focus();
			await ondone?.();
		} catch (err) {
			submitError = formatApiDetail(err instanceof Error ? err.message : undefined, action.failed);
		} finally {
			submitting = false;
		}
	}
</script>

<section class="erp-bill-lookup" data-testid={idPrefix} aria-labelledby="{idPrefix}-title">
	<h3 id="{idPrefix}-title">{labels.title}</h3>
	<p class="hint">{labels.intro}</p>

	<!-- One live region for the whole result chain, mounted from first paint so
	     a screen reader hears each step (searching, found, none, refused, the
	     commit's note); a failure is an alert of its own below. -->
	<div class="result" role="status" data-testid="{idPrefix}-status">
		{#if lookup.kind === 'loading'}
			<p class="hint">{labels.finding}</p>
		{:else if lookup.kind === 'found'}
			<p class="found" data-testid="{idPrefix}-found">
				{labels.found(billNumber(lookup.bill))}
				{#if lookup.bill.amount !== null}
					<Money amount={lookup.bill.amount} currency={lookup.bill.currency} />
				{/if}
			</p>
			<dl class="facts">
				<dt>{labels.statusLabel}</dt>
				<dd><code data-testid="{idPrefix}-erp-status">{lookup.bill.erp_status}</code></dd>
				{#if lookup.bill.erp_document_number}
					<!-- The ERP's own id, only when it differs from the number
					     already named above. -->
					<dt>{labels.idLabel}</dt>
					<dd><code data-testid="{idPrefix}-erp-id">{lookup.bill.erp_document_id}</code></dd>
				{/if}
			</dl>
			{@render afterFound?.()}
		{:else if lookup.kind === 'none'}
			<p class="hint" data-testid="{idPrefix}-none">{labels.none}</p>
		{:else if lookup.kind === 'refused'}
			<RefusalNote id="{idPrefix}-refusal" testid="{idPrefix}-refusal" reason={lookup.message} />
		{:else if note}
			<p class="hint" data-testid="{idPrefix}-voided">{note}</p>
		{/if}
	</div>
	{#if lookup.kind === 'error'}
		<p class="error" role="alert" data-testid="{idPrefix}-error">{lookup.message}</p>
	{/if}
	{#if refusal}
		<!-- The served verdict refuses the search: said beside the disabled
		     control, not discovered from a 409. -->
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
			{lookup.kind === 'loading' ? labels.finding : labels.find}
		</button>
		{#if lookup.kind === 'found'}
			<button
				type="button"
				class="btn-{action.tone}"
				data-testid="{idPrefix}-{action.testIds.open}"
				onclick={openConfirm}
			>
				{action.openLabel}
			</button>
		{/if}
	</div>
</section>

<Modal
	open={confirmOpen}
	ariaLabel={action.confirmTitle}
	width="sm"
	onclose={() => (confirmOpen = false)}
>
	<h2>{action.confirmTitle}</h2>
	{#if lookup.kind === 'found'}
		<p class="hint">
			<code>{billNumber(lookup.bill)}</code>
			{#if lookup.bill.amount !== null}
				· <Money amount={lookup.bill.amount} currency={lookup.bill.currency} />
			{/if}
		</p>
	{/if}
	<p class="hint">{action.confirmBody}</p>
	<form
		data-testid="{idPrefix}-{action.testIds.form}"
		onsubmit={(e) => {
			e.preventDefault();
			commit();
		}}
	>
		<label>
			<span>{action.reasonLabel}</span>
			<input
				type="text"
				bind:value={reason}
				maxlength="500"
				required
				data-autofocus
				data-testid="{idPrefix}-{action.testIds.reason}"
			/>
		</label>
		{#if submitError}
			<p class="error" role="alert" data-testid="{idPrefix}-{action.testIds.error}">
				{submitError}
			</p>
		{/if}
		<div class="actions">
			<button type="button" class="btn-cancel" onclick={() => (confirmOpen = false)}>
				{m('common.cancel')}
			</button>
			<button
				type="submit"
				class="btn-{action.tone}"
				data-testid="{idPrefix}-{action.testIds.submit}"
				disabled={submitting || body === null}
			>
				{submitting ? action.submitting : action.submit}
			</button>
		</div>
	</form>
</Modal>

<style>
	.erp-bill-lookup {
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
	/* The wrapper's `afterFound` snippet renders in its own scope. */
	.result :global(.hint) {
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
