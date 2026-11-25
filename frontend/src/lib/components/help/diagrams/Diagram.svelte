<script module lang="ts">
	import type { Component } from 'svelte';
	import type { DiagramId } from '#lib/help/types.ts';

	// Each drawing is its own lazy chunk: a guide shows one or two of the
	// eight, and bundled together they were ~12 KB on /help/guides/[id], the
	// route the web bundle budget gates. The guide page's `load` awaits
	// `loadDiagrams` for the ids its guide uses, so the drawing is in hand on
	// the first frame and nothing waits on it after render.
	const DIAGRAMS: Record<DiagramId, () => Promise<{ default: Component }>> = {
		'capture-channels': () => import('./CaptureChannels.svelte'),
		'three-way-match': () => import('./ThreeWayMatch.svelte'),
		'approval-chain': () => import('./ApprovalChain.svelte'),
		'segregation-of-duties': () => import('./SegregationOfDuties.svelte'),
		'bank-change-dual-control': () => import('./BankChangeDualControl.svelte'),
		'payment-run': () => import('./PaymentRun.svelte'),
		'exception-flow': () => import('./ExceptionFlow.svelte'),
		'roles-overview': () => import('./RolesOverview.svelte')
	};

	const loaded = new Map<DiagramId, Component>();

	function drawingFor(id: DiagramId): Component | Promise<Component> {
		return (
			loaded.get(id) ??
			DIAGRAMS[id]().then(({ default: c }) => {
				loaded.set(id, c);
				return c;
			})
		);
	}

	/** Fetch the drawings `ids` name, before the page that shows them renders. */
	export async function loadDiagrams(ids: Iterable<DiagramId>): Promise<void> {
		await Promise.all([...new Set(ids)].map((id) => drawingFor(id)));
	}
</script>

<script lang="ts">
	/**
	 * Renders the guide illustration `id` (see DIAGRAM_IDS in #lib/help/types.ts).
	 *
	 * Each diagram draws itself twice: a landscape `svg.d-wide` and a portrait
	 * `svg.d-tall`. A landscape picture scaled down to a phone would shrink its
	 * text to a few pixels, so below 40rem of guide column the portrait one is
	 * shown instead — a container query on the help centre's `help-main`, so it
	 * follows the column, not the window. The hidden copy is `display: none`,
	 * which also takes it out of the accessibility tree; each carries the same
	 * translated aria-label.
	 *
	 * A drawing `loadDiagrams` already fetched renders synchronously (an
	 * `{#await}` on a plain value takes the `then` branch at once).
	 */
	let { id }: { id: DiagramId } = $props();

	const drawing = $derived(drawingFor(id));
</script>

<div class="diagram" data-diagram={id}>{#await drawing then Drawing}<Drawing />{/await}</div>

<style>
	.diagram :global(svg) {
		display: block;
		overflow: visible;
		font-family: inherit;
	}
	.diagram :global(svg.d-tall) {
		display: none;
		max-width: 22rem;
		margin-inline: auto;
	}
	@container help-main (max-width: 40rem) {
		.diagram :global(svg.d-wide) {
			display: none;
		}
		.diagram :global(svg.d-tall) {
			display: block;
		}
	}
</style>
