<script lang="ts">
	/**
	 * Organization → Line-total check (decisions §502).
	 *
	 * Turns `settings.fraud_rules.line_total_mismatch_enabled` on or off: the
	 * check that an invoice's line totals add up to its header amount
	 * (`backend/docs/line-total-reconciliation.md`). Its one writer is the
	 * audited `PUT /api/organization/line-total-check`; the Fraud Detection
	 * form leaves the key out of its own save.
	 *
	 * Admin-only end to end: `readOnly` renders the notice and makes no read.
	 */
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { getLineTotalCheck, saveLineTotalCheck } from '#lib/api/orgControls.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';

	let {
		readOnly,
		userLoaded
	}: {
		/** The caller is not an admin: render the notice, read nothing. */
		readOnly: boolean;
		/** `/auth/me` has resolved, so `readOnly` is final. */
		userLoaded: boolean;
	} = $props();

	const uid = $props.id();

	let stored = $state<boolean | null>(null);
	let enabled = $state(true);
	let loading = $state(false);
	let loadFailed = $state(false);
	let saving = $state(false);
	let requested = false;

	const dirty = $derived(stored !== null && enabled !== stored);

	async function load() {
		loading = true;
		loadFailed = false;
		try {
			const res = await getLineTotalCheck();
			stored = res.enabled;
			enabled = res.enabled;
		} catch {
			loadFailed = true;
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		if (requested || !userLoaded || readOnly) return;
		requested = true;
		void load();
	});

	async function save() {
		saving = true;
		try {
			const res = await saveLineTotalCheck(enabled);
			stored = res.enabled;
			enabled = res.enabled;
			toast(m('org.lineTotal.saved'), 'success');
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			saving = false;
		}
	}
</script>

<section class="card" aria-labelledby="{uid}-title" data-testid="line-total-panel">
	<h2 id="{uid}-title">{m('org.section.lineTotal')}</h2>
	<p class="card-hint">{m('org.lineTotal.intro')}</p>

	{#if readOnly}
		<p class="card-hint" data-testid="line-total-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else if loading || (stored === null && !loadFailed)}
		<p class="card-hint" role="status">{m('org.loading')}</p>
	{:else if loadFailed}
		<LoadError message={m('org.lineTotal.loadFailed')} onretry={load} testId="line-total-load-error" />
	{:else}
		<label class="switch-row">
			<input
				type="checkbox"
				bind:checked={enabled}
				disabled={saving}
				aria-describedby="{uid}-effect"
				data-testid="line-total-toggle"
			/>
			<span>{m('org.lineTotal.toggle')}</span>
		</label>
		<p id="{uid}-effect" class="field-hint">{m('org.lineTotal.effect')}</p>
		{#if !enabled}
			<p class="off-warning" data-testid="line-total-off-warning">{m('org.lineTotal.offWarning')}</p>
		{/if}
		<div class="section-footer">
			<button
				type="button"
				class="btn-save-section"
				disabled={saving || !dirty}
				onclick={save}
				data-testid="line-total-save"
			>
				{saving ? m('org.common.saving') : m('org.lineTotal.save')}
			</button>
		</div>
	{/if}
</section>

<style>
	/* The panel frame the sibling organization panels use
	   (`CloseWorkspacePanel.svelte`). */
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 20px 24px;
	}

	.card h2 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0 0 4px;
	}

	.card-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	.switch-row {
		display: flex;
		align-items: center;
		gap: 10px;
		min-height: 24px;
		font-size: 0.9rem;
		color: var(--text);
		cursor: pointer;
	}

	.switch-row input[type='checkbox'] {
		width: 16px;
		height: 16px;
		accent-color: var(--accent);
		cursor: pointer;
		flex-shrink: 0;
	}

	.field-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 4px 0 0 26px;
	}

	/* The page's `.chat-warning` tint pair (contrast measured on `--warning-on-tint`). */
	.off-warning {
		margin: 12px 0 0;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.section-footer {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		margin-top: 16px;
	}

	.btn-save-section {
		padding: 8px 18px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-save-section:hover:not(:disabled) {
		opacity: 0.85;
	}

	.btn-save-section:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
