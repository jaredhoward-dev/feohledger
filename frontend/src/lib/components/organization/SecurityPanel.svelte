<script lang="ts">
	/**
	 * Organization → Security: the org-wide MFA requirement. The toggle is the
	 * page's own `mfaRequired`, bound in, so an unsaved flip survives a panel
	 * switch; the page owns the save and the "is enforcement live?" answer.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		mfaRequired = $bindable(),
		enforcementActive,
		readOnly,
		saving,
		onsave
	}: {
		mfaRequired: boolean;
		/** The server is enforcing it — false shows the "not yet active" warning. */
		enforcementActive: boolean;
		readOnly: boolean;
		saving: boolean;
		onsave: () => void;
	} = $props();
</script>

<section class="card org-panel">
	<h2>{m('org.section.security')}</h2>
	<p class="card-hint">
		{m('org.security.hint')}
	</p>
	{#if readOnly}
		<p class="card-hint" data-testid="security-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else}
		<label class="switch-row">
			<input type="checkbox" bind:checked={mfaRequired} />
			<span>{m('org.security.requireMfa')}</span>
		</label>

		{#if mfaRequired && !enforcementActive}
			<p class="mfa-enforcement-warning" role="alert" data-testid="mfa-enforcement-inactive">
				{m('org.security.mfaEnforcementInactive')}
			</p>
		{/if}

		<div class="section-footer">
			<button class="btn-save-section" disabled={saving} onclick={onsave}>
				{saving ? m('org.common.saving') : m('org.security.save')}
			</button>
		</div>
	{/if}
</section>

<style>
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

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	/* The field label only — not the .color-field wrapper span (reset below). */
	label > span:first-child {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	label.switch-row {
		flex-direction: row;
		align-items: center;
		gap: 10px;
		font-size: 0.9rem;
		color: var(--text);
		cursor: pointer;
	}

	label.switch-row span {
		font-size: 0.9rem;
		font-weight: 400;
		color: var(--text);
		text-transform: none;
		letter-spacing: normal;
	}

	label.switch-row input[type='checkbox'] {
		width: 16px;
		height: 16px;
		accent-color: var(--accent);
		cursor: pointer;
		flex-shrink: 0;
	}

	.mfa-enforcement-warning {
		color: var(--warning-on-tint);
		font-size: 0.82rem;
		font-weight: 600;
		margin: 8px 0 0;
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
