<script lang="ts">
	/**
	 * Organization → Exception agents: the autonomy level (decisions §502).
	 *
	 * `settings.exception_agents.autonomy_level` decides when an exception
	 * agent applies its own fix instead of escalating to a person
	 * (`backend/docs/exception-agents.md` § Autonomy → threshold). Above
	 * `conservative` an agent may change an invoice — its amount to the PO
	 * total, its PO link, its GL account — and approve it, as the user who ran
	 * it, so the panel states that beside the choice. Its one writer is the
	 * audited `PUT /api/organization/exception-agents/autonomy`.
	 *
	 * Admin-only end to end: `readOnly` renders the notice and makes no read.
	 */
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { getAgentAutonomy, saveAgentAutonomy } from '#lib/api/orgControls.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import {
		AUTONOMY_LEVELS,
		autonomyHintKey,
		autonomyLabelKey,
		type AutonomyLevel
	} from '#lib/types/orgControls.ts';
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

	let stored = $state<AutonomyLevel | null>(null);
	let level = $state<AutonomyLevel>('conservative');
	let loading = $state(false);
	let loadFailed = $state(false);
	let saving = $state(false);
	let requested = false;

	const dirty = $derived(stored !== null && level !== stored);

	async function load() {
		loading = true;
		loadFailed = false;
		try {
			const res = await getAgentAutonomy();
			stored = res.autonomy_level;
			level = res.autonomy_level;
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
			const res = await saveAgentAutonomy(level);
			stored = res.autonomy_level;
			level = res.autonomy_level;
			toast(m('org.agentAutonomy.saved'), 'success');
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			saving = false;
		}
	}
</script>

<section class="card" aria-labelledby="{uid}-title" data-testid="agent-autonomy-panel">
	<h2 id="{uid}-title">{m('org.section.agentAutonomy')}</h2>
	<p class="card-hint">{m('org.agentAutonomy.intro')}</p>

	{#if readOnly}
		<p class="card-hint" data-testid="agent-autonomy-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else if loading || (stored === null && !loadFailed)}
		<p class="card-hint" role="status">{m('org.loading')}</p>
	{:else if loadFailed}
		<LoadError
			message={m('org.agentAutonomy.loadFailed')}
			onretry={load}
			testId="agent-autonomy-load-error"
		/>
	{:else}
		<p class="what-it-does" id="{uid}-scope">{m('org.agentAutonomy.whatItDoes')}</p>
		<fieldset aria-describedby="{uid}-scope" disabled={saving}>
			<legend>{m('org.agentAutonomy.legend')}</legend>
			{#each AUTONOMY_LEVELS as option (option)}
				<label class="option">
					<input
						type="radio"
						name="{uid}-level"
						value={option}
						bind:group={level}
						data-testid="agent-autonomy-{option}"
					/>
					<span>
						<strong>{m(autonomyLabelKey(option))}</strong>
						<span class="option-hint">{m(autonomyHintKey(option))}</span>
					</span>
				</label>
			{/each}
		</fieldset>
		<div class="section-footer">
			<button
				type="button"
				class="btn-save-section"
				disabled={saving || !dirty}
				onclick={save}
				data-testid="agent-autonomy-save"
			>
				{saving ? m('org.common.saving') : m('org.agentAutonomy.save')}
			</button>
		</div>
	{/if}
</section>

<style>
	/* The panel frame the sibling organization panels use
	   (`CloseWorkspacePanel.svelte`, `LineTotalCheckPanel.svelte`). */
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

	fieldset {
		border: none;
		margin: 0;
		padding: 0;
		min-width: 0;
	}

	legend {
		font-size: 0.85rem;
		font-weight: 600;
		margin-bottom: 8px;
		padding: 0;
	}

	.option {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		min-height: 24px;
		padding: 6px 0;
		cursor: pointer;
	}

	.option input[type='radio'] {
		width: 16px;
		height: 16px;
		margin-top: 2px;
		accent-color: var(--accent);
		cursor: pointer;
		flex-shrink: 0;
	}

	.option strong {
		display: block;
		font-size: 0.9rem;
		font-weight: 500;
		color: var(--text);
	}

	.option-hint {
		display: block;
		margin-top: 2px;
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.what-it-does {
		font-size: 0.82rem;
		color: var(--text);
		margin: 0 0 14px;
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
