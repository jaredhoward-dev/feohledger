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
	import OrgSettingPanel from '#lib/components/organization/OrgSettingPanel.svelte';
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

<OrgSettingPanel
	title={m('org.section.agentAutonomy')}
	intro={m('org.agentAutonomy.intro')}
	testId="agent-autonomy"
	{readOnly}
	loading={loading || (stored === null && !loadFailed)}
	{loadFailed}
	loadFailedMessage={m('org.agentAutonomy.loadFailed')}
	onretry={load}
>
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
	{#snippet footer()}
		<button
			type="button"
			class="btn-primary"
			disabled={saving || !dirty}
			onclick={save}
			data-testid="agent-autonomy-save"
		>
			{saving ? m('org.common.saving') : m('org.agentAutonomy.save')}
		</button>
	{/snippet}
</OrgSettingPanel>

<style>
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
</style>
