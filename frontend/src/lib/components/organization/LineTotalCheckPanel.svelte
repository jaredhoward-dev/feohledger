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
	import OrgSettingPanel from '#lib/components/organization/OrgSettingPanel.svelte';
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

<OrgSettingPanel
	title={m('org.section.lineTotal')}
	intro={m('org.lineTotal.intro')}
	testId="line-total"
	{readOnly}
	loading={loading || (stored === null && !loadFailed)}
	{loadFailed}
	loadFailedMessage={m('org.lineTotal.loadFailed')}
	onretry={load}
>
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
	{#snippet footer()}
		<button
			type="button"
			class="btn-primary"
			disabled={saving || !dirty}
			onclick={save}
			data-testid="line-total-save"
		>
			{saving ? m('org.common.saving') : m('org.lineTotal.save')}
		</button>
	{/snippet}
</OrgSettingPanel>

<style>
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
</style>
