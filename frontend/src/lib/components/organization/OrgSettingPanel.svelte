<script lang="ts">
	/**
	 * The frame of a single-setting Organization panel: the titled card, its
	 * intro line, the admin-only notice, and the four async states around the
	 * form the caller supplies.
	 *
	 * A panel that loads one setting and saves it through its own audited
	 * writer (`LineTotalCheckPanel`, `AgentAutonomyPanel`) is the same frame
	 * every time; this owns it so the card CSS, the loading region
	 * (`LoadingState`, a polite status — `a11y/liveRegionAudit.test.ts`) and
	 * the load-failure retry (`LoadError`) are written once. The save button
	 * is the global `.btn-primary`, placed by the caller inside `footer`.
	 *
	 * `testId` is the prefix of every hook: `<testId>-panel`,
	 * `<testId>-admin-only`, `<testId>-loading`, `<testId>-load-error`.
	 *
	 * Not for the multi-step connection panels (`ErpConnectionPanel`,
	 * `CommerceConnectionPanel`): they carry their own picker / mapping states.
	 */
	import type { Snippet } from 'svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		title,
		intro,
		testId,
		readOnly,
		loading,
		loadFailed,
		loadFailedMessage,
		onretry,
		children,
		footer
	}: {
		/** The panel heading, localized. */
		title: string;
		/** One line on what the setting does, localized. */
		intro: string;
		/** Prefix for every `data-testid` the frame renders. */
		testId: string;
		/** The caller is not an admin: the notice instead of the form. */
		readOnly: boolean;
		/** The setting has not arrived yet. */
		loading: boolean;
		/** The read failed: the retry block instead of the form. */
		loadFailed: boolean;
		/** What failed to load, localized. */
		loadFailedMessage: string;
		/** Re-runs the read. */
		onretry: () => void;
		/** The form, rendered once the setting has loaded. */
		children: Snippet;
		/** The save row under the form. */
		footer?: Snippet;
	} = $props();

	const uid = $props.id();
</script>

<section class="card" aria-labelledby="{uid}-title" data-testid="{testId}-panel">
	<h2 id="{uid}-title">{title}</h2>
	<p class="card-hint">{intro}</p>

	{#if readOnly}
		<p class="card-hint" data-testid="{testId}-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else if loading}
		<LoadingState label={m('org.loading')} testId="{testId}-loading" />
	{:else if loadFailed}
		<LoadError message={loadFailedMessage} {onretry} testId="{testId}-load-error" />
	{:else}
		{@render children()}
		{#if footer}
			<div class="section-footer">{@render footer()}</div>
		{/if}
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

	.section-footer {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		margin-top: 16px;
	}
</style>
