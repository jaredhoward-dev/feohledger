<script lang="ts">
	/**
	 * Profile → Out of office: the caller's own approval delegation
	 * (`GET/POST/DELETE /api/auth/delegation`, decisions §511).
	 *
	 * A personal setting, not an organization one — the endpoint writes the
	 * caller's own row and is gated on sign-in alone, so every employee sees
	 * this panel. Who may be named comes from `/api/auth/delegation/candidates`,
	 * which shares the write's eligibility test, so the list never offers a
	 * colleague the save would refuse. Refusals arrive coded and are stated in
	 * the reader's language (`api/authRefusals.ts`).
	 */
	import { onMount } from 'svelte';
	import { api } from '#lib/api.ts';
	import { authErrorMessage } from '#lib/api/authRefusals.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { formatDate } from '#lib/utils/time.ts';
	import {
		delegationState,
		lastDayCovered,
		localDay,
		untilAfterLastDay,
		type DelegateCandidate,
		type Delegation
	} from '#lib/utils/delegation.ts';

	let delegation = $state<Delegation | null>(null);
	let candidates = $state<DelegateCandidate[]>([]);
	let loaded = $state(false);
	let loadError = $state<string | null>(null);
	let busy = $state(false);

	let delegateId = $state('');
	let lastDay = $state('');
	let delegateError = $state<string | null>(null);
	let lastDayError = $state<string | null>(null);
	let formError = $state<string | null>(null);

	const today = localDay(new Date());
	const status = $derived(delegationState(delegation));
	const shownDay = $derived(formatDate(lastDayCovered(delegation?.until ?? null)));

	function adoptCurrent(d: Delegation) {
		delegation = d;
		if (delegationState(d) === 'active') {
			delegateId = d.delegate_to_id ?? '';
			lastDay = lastDayCovered(d.until) ?? '';
		}
	}

	async function load() {
		loadError = null;
		try {
			const [current, people] = await Promise.all([
				api.get<Delegation>('/api/auth/delegation'),
				api.get<DelegateCandidate[]>('/api/auth/delegation/candidates')
			]);
			candidates = people;
			adoptCurrent(current);
		} catch (err) {
			loadError = authErrorMessage(err, m, 'profile.delegation.loadFailed');
		} finally {
			loaded = true;
		}
	}

	onMount(load);

	async function save(event: SubmitEvent) {
		event.preventDefault();
		formError = null;
		delegateError = delegateId ? null : m('profile.delegation.delegateRequired');
		const until = lastDay && lastDay >= today ? untilAfterLastDay(lastDay) : null;
		lastDayError = until ? null : m('profile.delegation.lastDayRequired');
		if (delegateError || lastDayError || !until) return;

		busy = true;
		try {
			const saved = await api.post<Delegation>('/api/auth/delegation', {
				delegate_to_id: delegateId,
				until
			});
			adoptCurrent(saved);
			toast(m('profile.delegation.saved'), 'success');
		} catch (err) {
			formError = authErrorMessage(err, m, 'profile.delegation.saveFailed');
		} finally {
			busy = false;
		}
	}

	async function end() {
		formError = null;
		busy = true;
		try {
			await api.delete('/api/auth/delegation');
			delegation = { delegate_to_id: null, delegate_to_name: null, until: null, is_active: false };
			delegateId = '';
			lastDay = '';
			toast(m('profile.delegation.cleared'), 'success');
		} catch (err) {
			formError = authErrorMessage(err, m, 'profile.delegation.endFailed');
		} finally {
			busy = false;
		}
	}
</script>

<section class="card" aria-labelledby="delegation-heading" data-testid="delegation-panel">
	<h2 id="delegation-heading">{m('profile.delegation.heading')}</h2>
	<p class="hint">{m('profile.delegation.hint')}</p>
	<p class="hint">{m('profile.delegation.limits')}</p>

	{#if !loaded}
		<LoadingState />
	{:else if loadError}
		<p class="error" role="alert">{loadError}</p>
	{:else}
		<p class="status" class:on={status === 'active'} data-testid="delegation-status" aria-live="polite">
			{#if status === 'active'}
				{m('profile.delegation.statusOn', {
					name: delegation?.delegate_to_name ?? '—',
					date: shownDay
				})}
			{:else if status === 'ended'}
				{m('profile.delegation.statusEnded', {
					name: delegation?.delegate_to_name ?? '—',
					date: shownDay
				})}
			{:else}
				{m('profile.delegation.statusOff')}
			{/if}
		</p>

		{#if candidates.length === 0 && status !== 'active'}
			<p class="hint" data-testid="delegation-no-candidates">
				{m('profile.delegation.noCandidates')}
			</p>
		{:else}
			<form onsubmit={save} novalidate aria-describedby={formError ? 'delegation-form-error' : undefined}>
				<label>
					<span>{m('profile.delegation.delegate')}</span>
					<select
						bind:value={delegateId}
						aria-invalid={delegateError ? 'true' : undefined}
						aria-describedby={delegateError ? 'delegation-delegate-error' : undefined}
						data-testid="delegation-delegate"
					>
						<option value="" disabled>{m('profile.delegation.choose')}</option>
						{#each candidates as c (c.id)}
							<option value={c.id}>{c.full_name}</option>
						{/each}
					</select>
				</label>
				{#if delegateError}
					<p class="field-error" id="delegation-delegate-error">{delegateError}</p>
				{/if}

				<label>
					<span>{m('profile.delegation.lastDay')}</span>
					<input
						type="date"
						min={today}
						bind:value={lastDay}
						aria-invalid={lastDayError ? 'true' : undefined}
						aria-describedby={lastDayError
							? 'delegation-last-day-hint delegation-last-day-error'
							: 'delegation-last-day-hint'}
						data-testid="delegation-last-day"
					/>
				</label>
				<p class="field-hint" id="delegation-last-day-hint">{m('profile.delegation.lastDayHint')}</p>
				{#if lastDayError}
					<p class="field-error" id="delegation-last-day-error">{lastDayError}</p>
				{/if}

				{#if formError}
					<p class="error" id="delegation-form-error" role="alert">{formError}</p>
				{/if}

				<div class="actions">
					<button type="submit" disabled={busy} data-testid="delegation-save">
						{status === 'active' ? m('profile.delegation.update') : m('profile.delegation.turnOn')}
					</button>
					{#if status !== 'none'}
						<button
							type="button"
							class="secondary"
							disabled={busy}
							onclick={end}
							data-testid="delegation-end"
						>
							{m('profile.delegation.end')}
						</button>
					{/if}
				</div>
			</form>
		{/if}
	{/if}
</section>

<style>
	/* The profile page's card recipe; its rules are scoped to that page, so
	   this panel carries its own copy rather than reaching in with :global. */
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 24px;
	}

	h2 {
		margin: 0 0 12px;
		font-size: 1rem;
		font-weight: 600;
	}

	.hint {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 0 0 12px;
	}

	.status {
		display: inline-flex;
		padding: 4px 10px;
		border-radius: 4px;
		font-size: 0.82rem;
		font-weight: 500;
		margin: 4px 0 16px;
		background: var(--bg);
		color: var(--text-muted);
	}

	.status.on {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	form {
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-width: 28rem;
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}

	select,
	input {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
		font-family: inherit;
	}

	select[aria-invalid='true'],
	input[aria-invalid='true'] {
		border-color: var(--danger);
	}

	.field-hint {
		margin: 0;
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.field-error,
	.error {
		margin: 0;
		font-size: 0.82rem;
		color: var(--danger-on-tint);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 8px;
	}

	button {
		padding: 9px 16px;
		border-radius: 4px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.88rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	button.secondary {
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text);
	}
</style>
