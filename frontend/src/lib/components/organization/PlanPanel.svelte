<script lang="ts">
	/** Organization → Plan: the tenant's plan, slug and creation date. Read-only. */
	import Badge from '#lib/components/ui/Badge.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { planCodeLabel } from '#lib/types/planFeatures.ts';
	import { formatDate } from '#lib/utils/time.ts';

	let { org }: { org: { plan: string; slug: string; created_at: string } } = $props();

	// `org.plan` is the live subscription's plan code (decisions §482); a
	// negotiated plan the client has no name for shows its code.
	const planLabel = (plan: string): string => planCodeLabel(plan, m);
</script>

<section class="card plan-card">
	<h2>{m('org.section.plan')}</h2>
	<div class="plan-info">
		<Badge tone="accent" variant="plan-badge">{planLabel(org.plan)}</Badge>
		<span class="plan-slug">{m('org.plan.tenant')} <code>{org.slug}</code></span>
		<span class="plan-date">{m('org.plan.created', { date: formatDate(org.created_at, '—', { month: 'long', day: 'numeric', year: 'numeric' }) })}</span>
	</div>
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

	.plan-card h2 {
		margin-bottom: 12px;
	}

	.plan-info {
		display: flex;
		align-items: center;
		gap: 16px;
	}

	.plan-slug {
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.plan-slug code {
		background: var(--bg);
		padding: 2px 6px;
		border-radius: 3px;
		font-size: 0.8rem;
	}

	.plan-date {
		font-size: 0.82rem;
		color: var(--text-muted);
	}
</style>
