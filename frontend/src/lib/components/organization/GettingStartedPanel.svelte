<script lang="ts">
	/**
	 * Organization → Getting started: the first-time admin's checklist. The
	 * page computes the steps (it owns the onboarding read); this only lays
	 * them out.
	 */
	import SetupChecklist from '#lib/components/ui/SetupChecklist.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { ComponentProps } from 'svelte';

	let { steps }: { steps: ComponentProps<typeof SetupChecklist>['steps'] } = $props();
</script>

<section class="getting-started card">
	<h2 id="org-getting-started-heading">{m('org.gettingStarted.title')}</h2>
	<p class="card-hint">{m('org.gettingStarted.intro')}</p>
	<!-- These were `#org-*` anchors into one long scroll. They now name
	     the panel that owns each step directly; the old anchors still
	     resolve, via `LEGACY_HASH_SECTIONS`, for the copies of them
	     that exist outside this repo. -->
	<SetupChecklist
		{steps}
		labelledby="org-getting-started-heading"
		testId="org-getting-started-steps"
	/>
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

	/* First-time-admin wayfinding — links jump to the sections a new tenant
	   configures first. Purely a shortcut; nothing is hidden. */
	.getting-started {
		border-color: var(--accent);
	}
</style>
