<script lang="ts">
	/**
	 * Profile → Out of office: the caller's own approval delegation
	 * (`GET/POST/DELETE /api/auth/delegation`, decisions §511).
	 *
	 * A personal setting, not an organization one, and only for an approver:
	 * naming a delegate (`POST`) and listing candidates answer 403 to a caller
	 * without `invoice.approve` (there is nothing of theirs to hand over), so
	 * the profile page offers this section only when
	 * `auth.can('invoice.approve')`. `GET` and `DELETE` stay open, so a caller
	 * whose grant was removed since (another tab, a stale page) still sees a
	 * delegation left on record and can end it; the rest reads as
	 * approvers-only, not as a failure.
	 * Who may be named comes from `/api/auth/delegation/candidates`, which
	 * shares the write's eligibility test, so the list never offers a colleague
	 * the save would refuse. Refusals arrive coded and are stated in the
	 * reader's language (`api/authRefusals.ts`).
	 *
	 * The page owns the card and its heading (`profile/+page.svelte`); this
	 * renders the card's body.
	 */
	import { onMount, tick } from 'svelte';
	import { api, ApiError } from '#lib/api.ts';
	import { authErrorMessage } from '#lib/api/authRefusals.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
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
	let forbidden = $state(false);
	let busy = $state(false);

	let delegateId = $state('');
	let lastDay = $state('');
	let delegateError = $state<string | null>(null);
	let lastDayError = $state<string | null>(null);
	let formError = $state<string | null>(null);

	let delegateEl = $state<HTMLSelectElement | null>(null);
	let lastDayEl = $state<HTMLInputElement | null>(null);
	let statusEl = $state<HTMLParagraphElement | null>(null);

	const today = localDay(new Date());
	const status = $derived(delegationState(delegation));
	const shownDay = $derived(formatDate(lastDayCovered(delegation?.until ?? null)));

	// The live delegate stays named and selected even when they are no longer
	// a candidate (their approval grant was removed since): the select would
	// otherwise show "Choose a colleague" over a delegation that is still on.
	// Saving them again is the server's call, and its coded refusal says why.
	const options = $derived.by(() => {
		const current = delegation?.delegate_to_id;
		if (status !== 'active' || !current || candidates.some((c) => c.id === current)) {
			return candidates;
		}
		return [
			{ id: current, full_name: delegation?.delegate_to_name ?? '—' },
			...candidates
		];
	});

	function adoptCurrent(d: Delegation) {
		delegation = d;
		if (delegationState(d) === 'active') {
			delegateId = d.delegate_to_id ?? '';
			lastDay = lastDayCovered(d.until) ?? '';
		}
	}

	async function load() {
		loadError = null;
		forbidden = false;
		try {
			const [current, people] = await Promise.all([
				api.get<Delegation>('/api/auth/delegation'),
				api.get<DelegateCandidate[]>('/api/auth/delegation/candidates').catch((err) => {
					// Not an approver (any more): no one to name, but the record
					// itself is still readable and can still be ended.
					if (err instanceof ApiError && err.status === 403) return null;
					throw err;
				})
			]);
			forbidden = people === null;
			candidates = people ?? [];
			adoptCurrent(current);
		} catch (err) {
			if (err instanceof ApiError && err.status === 403) {
				forbidden = true;
				delegation = null;
			} else loadError = authErrorMessage(err, m, 'profile.delegation.loadFailed');
		} finally {
			loaded = true;
		}
	}

	function retryLoad() {
		loaded = false;
		void load();
	}

	onMount(load);

	async function save(event: SubmitEvent) {
		event.preventDefault();
		formError = null;
		delegateError = delegateId ? null : m('profile.delegation.delegateRequired');
		const until = lastDay && lastDay >= today ? untilAfterLastDay(lastDay) : null;
		lastDayError = until ? null : m('profile.delegation.lastDayRequired');
		if (delegateError || lastDayError || !until) {
			// A refused submit moves focus to the first field to fix, so the
			// error is announced (it is first in that field's description) and
			// the user is where they need to be (WCAG 3.3.1).
			await tick();
			(delegateError ? delegateEl : lastDayEl)?.focus();
			return;
		}

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
			delegateError = null;
			lastDayError = null;
			toast(m('profile.delegation.cleared'), 'success');
			// End unmounts the button that had focus; the status line, which now
			// reads "Off", takes it rather than <body> (WCAG 2.4.3).
			await tick();
			statusEl?.focus();
		} catch (err) {
			formError = authErrorMessage(err, m, 'profile.delegation.endFailed');
		} finally {
			busy = false;
		}
	}
</script>

<div data-testid="delegation-panel">
	<p class="hint">{m('profile.delegation.hint')}</p>
	<p class="hint">{m('profile.delegation.limits')}</p>

	{#if !loaded}
		<LoadingState />
	{:else if loadError}
		<LoadError message={loadError} onretry={retryLoad} testId="delegation-load-error" />
	{:else}
		<p
			class="status"
			class:on={status === 'active'}
			data-testid="delegation-status"
			aria-live="polite"
			tabindex="-1"
			bind:this={statusEl}
		>
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

		{#if formError}
			<p class="error" id="delegation-form-error" role="alert">{formError}</p>
		{/if}

		{#if forbidden}
			<!-- No longer an approver, with a delegation still on record: it
			     cannot be changed, only ended. -->
			<p class="hint" data-testid="delegation-approvers-only">
				{m('profile.delegation.approversOnly')}
			</p>
			{#if status !== 'none'}
				<div class="actions">
					<button
						type="button"
						class="btn-secondary"
						disabled={busy}
						onclick={end}
						data-testid="delegation-end"
					>
						{status === 'ended' ? m('profile.delegation.clearEnded') : m('profile.delegation.end')}
					</button>
				</div>
			{/if}
		{:else if options.length === 0}
			<p class="hint" data-testid="delegation-no-candidates">
				{m('profile.delegation.noCandidates')}
			</p>
			<!-- Nobody to name, but an ended delegation still on record can be
			     cleared, so it does not read "ended" here indefinitely. -->
			{#if status === 'ended'}
				<div class="actions">
					<button
						type="button"
						class="btn-secondary"
						disabled={busy}
						onclick={end}
						data-testid="delegation-end"
					>
						{m('profile.delegation.clearEnded')}
					</button>
				</div>
			{/if}
		{:else}
			<form onsubmit={save} novalidate aria-describedby={formError ? 'delegation-form-error' : undefined}>
				<label>
					<span>{m('profile.delegation.delegate')}</span>
					<select
						bind:this={delegateEl}
						bind:value={delegateId}
						aria-invalid={delegateError ? 'true' : undefined}
						aria-describedby={delegateError ? 'delegation-delegate-error' : undefined}
						data-testid="delegation-delegate"
					>
						<option value="" disabled>{m('profile.delegation.choose')}</option>
						{#each options as c (c.id)}
							<option value={c.id}>{c.full_name}</option>
						{/each}
					</select>
				</label>
				{#if delegateError}
					<p class="field-error" id="delegation-delegate-error">{delegateError}</p>
				{/if}

				<label>
					<span>{m('profile.delegation.lastDay')}</span>
					<!-- The error id comes FIRST in the description, so a screen
					     reader reads what is wrong before the standing hint. -->
					<input
						bind:this={lastDayEl}
						type="date"
						min={today}
						bind:value={lastDay}
						aria-invalid={lastDayError ? 'true' : undefined}
						aria-describedby={lastDayError
							? 'delegation-last-day-error delegation-last-day-hint'
							: 'delegation-last-day-hint'}
						data-testid="delegation-last-day"
					/>
				</label>
				<p class="field-hint" id="delegation-last-day-hint">{m('profile.delegation.lastDayHint')}</p>
				{#if lastDayError}
					<p class="field-error" id="delegation-last-day-error">{lastDayError}</p>
				{/if}

				<div class="actions">
					<button type="submit" class="btn-primary" disabled={busy} data-testid="delegation-save">
						{status === 'active' ? m('profile.delegation.update') : m('profile.delegation.turnOn')}
					</button>
					{#if status !== 'none'}
						<button
							type="button"
							class="btn-secondary"
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
</div>

<style>
	/* The card and its heading are the profile page's (`.card` there); this
	   styles only the body. Buttons are the shared `.btn-primary` /
	   `.btn-secondary` (app.css). */
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
		margin: 0 0 8px;
		font-size: 0.82rem;
		color: var(--danger-on-tint);
	}

	.field-error {
		margin: 0;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 8px;
	}
</style>
