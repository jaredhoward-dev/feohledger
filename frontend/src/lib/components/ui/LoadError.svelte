<script lang="ts">
	/**
	 * A failed load, stated in place, with a way back: the message and a Retry
	 * button. THE error-and-retry primitive — a page's failed fetch and the
	 * whole-shell account load (`layout/AccountLoadState`) both render this one,
	 * so the two cannot drift into different answers to the same situation.
	 *
	 * Render it where the content would have been, in the `{:else if}` after the
	 * success branch and before the loading line. A toast is not a substitute:
	 * it fades, and a page that only toasts its failure is left on its loading
	 * line forever — `/organization` read "Loading…" indefinitely after a failed
	 * `GET /api/organization` (#635).
	 *
	 * Every string arrives already localized, so each caller keeps its own key
	 * namespace. The button always reads `common.retry`: one label for one
	 * situation, everywhere (a panel that said "Try again" sent a spec looking
	 * for a button that did not exist).
	 *
	 * Two shapes:
	 *
	 * - **In a page** (no `heading`): `role="alert"`, because it replaces content
	 *   the reader was waiting for. Focus follows what Retry did to this block,
	 *   never a guess made before it ran (WCAG 2.4.3):
	 *   - The caller **unmounts** it (clears its error flag to show its loading
	 *     line, now or when the retry lands): the focused button vanishes and
	 *     focus would fall to `<body>`, back at the top of the document. So once
	 *     the button is gone, and only if focus really was lost with it, focus
	 *     moves to the enclosing `<main>` (the layout's skip-link target, already
	 *     `tabindex="-1"`) and the next Tab lands in the content that replaced
	 *     this. Inside a dialog (`ui/Modal`, also `tabindex="-1"`) the dialog
	 *     takes it instead: `<main>` is outside the focus trap.
	 *   - The caller **keeps it mounted** through the retry (`retrying`, as the
	 *     profile cards do): focus stays on the button the user pressed, which
	 *     reads `common.retrying` meanwhile.
	 *   A retry that fails again re-renders the message, so the alert is
	 *   announced again instead of sitting on unchanged text (WCAG 4.1.3). For
	 *   that the block counts attempts itself: `retrying` turning false while it
	 *   is still mounted, or the promise `onretry` returned settling with it
	 *   still mounted, is a repeated failure.
	 * - **Replacing the whole screen** (`heading` given): a titled region whose
	 *   heading the caller focuses through {@link focusHeading}, which is what a
	 *   screen reader announces — so no `role="alert"` on top of it, which would
	 *   read the same words twice. The caller keeps the block mounted through a
	 *   retry and says so with `retrying`: the button turns `aria-disabled` (not
	 *   `disabled`, which would throw focus to `<body>`) and reads
	 *   `common.retrying`, and keeps the focus the user pressed it with.
	 *
	 * `actions` renders after Retry, for a second way out (the account panel's
	 * Log out).
	 */
	import { tick, type Snippet } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		message,
		onretry,
		heading,
		headingLevel = 2,
		retrying = false,
		actions,
		testId
	}: {
		message: string;
		/** Run the retry. Return its promise when the block may stay mounted
		 *  until it settles, so focus and the re-announcement follow the result. */
		onretry: () => unknown;
		/** A title for the failure. Omit inside a page; pass when this replaces the screen. */
		heading?: string;
		headingLevel?: 1 | 2 | 3;
		/** A retry is in flight and this block stays mounted through it. */
		retrying?: boolean;
		/** Further buttons after Retry. */
		actions?: Snippet;
		/** `data-testid` for an e2e selector; the class is Svelte-scoped. */
		testId?: string;
	} = $props();

	let headingEl = $state<HTMLElement | null>(null);
	const headingId = $props.id();

	/** Move focus onto the heading — call it when the failure (re)appears. */
	export function focusHeading() {
		headingEl?.focus();
	}

	/** Bumped on each failed retry that leaves this block mounted; the message
	 *  is keyed on it so the alert re-announces the same words. */
	let attempt = $state(0);
	let wasRetrying = false;
	$effect(() => {
		const now = retrying;
		if (wasRetrying && !now) attempt += 1;
		wasRetrying = now;
	});

	/** Focus was lost with the button (it unmounted while focused). */
	function focusLost(): boolean {
		const active = document.activeElement;
		return active === null || active === document.body;
	}

	async function retry(e: MouseEvent) {
		if (retrying) return;
		const button = e.currentTarget as HTMLElement;
		// Noted now: once the block unmounts the button has no ancestors left.
		const container = heading
			? null
			: button.closest<HTMLElement>('[role="dialog"], main');
		const before = attempt;
		const result = onretry();
		await tick();
		if (!button.isConnected) {
			if (focusLost()) container?.focus();
			return;
		}
		if (!(result instanceof Promise)) return;
		await result.catch(() => {});
		await tick();
		if (!button.isConnected) {
			if (focusLost()) container?.focus();
		} else if (attempt === before) {
			// Still here once the retry settled: it failed again. A caller that
			// drives `retrying` was already counted by the effect.
			attempt += 1;
		}
	}
</script>

{#snippet body()}
	{#key attempt}
		<p class="message">{message}</p>
	{/key}
	<div class="actions">
		<button type="button" class="btn-primary" onclick={retry} aria-disabled={retrying || undefined}>
			{retrying ? m('common.retrying') : m('common.retry')}
		</button>
		{@render actions?.()}
	</div>
{/snippet}

{#if heading}
	<section class="load-error titled" aria-labelledby={headingId} data-testid={testId}>
		<svelte:element
			this={`h${headingLevel}`}
			id={headingId}
			tabindex="-1"
			bind:this={headingEl}
		>
			{heading}
		</svelte:element>
		{@render body()}
	</section>
{:else}
	<div class="load-error" role="alert" data-testid={testId}>
		{@render body()}
	</div>
{/if}

<style>
	.load-error {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 12px;
		color: var(--text-muted);
		padding: 24px 0;
	}

	.load-error.titled {
		padding: 0;
	}

	.titled > :global(:is(h1, h2, h3)) {
		margin: 4px 0 0;
		font-size: 1.25rem;
		font-weight: 700;
		color: var(--text);
	}

	.message {
		margin: 0;
	}

	.titled .message {
		font-size: 0.93rem;
		line-height: 1.55;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
	}

	.titled .actions {
		margin-top: 8px;
	}

	/* :global so a button the caller passes in `actions` (rendered in the
	   caller's scope) gets the busy cursor too, not only Retry. */
	.actions :global(button[aria-disabled='true']) {
		cursor: progress;
	}
</style>
