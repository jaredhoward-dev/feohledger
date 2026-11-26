<script lang="ts">
	import { page } from '$app/state';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { groupForPath, sectionTabActive, visibleChildren, type NavChild } from '#lib/nav.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { splitSectionTabs } from '#lib/sectionTabsSplit.ts';

	// The grouped section (Procurement / Billing / Automation / Governance /
	// Settings / Insights) that owns the current route, if any. Top-level links
	// (Invoices, Payments, …) return null → no sub-tab bar. Children the current
	// role can't see are dropped.
	let group = $derived(groupForPath(page.url.pathname));
	let tabs = $derived(
		group ? visibleChildren(group, auth.hasAnyRole.bind(auth), auth.can.bind(auth)) : []
	);
	let activeIndex = $derived(tabs.findIndex((t) => sectionTabActive(t, tabs, page.url)));

	/** Must match the `gap` on `.section-tabs-row` / `.section-tabs-measure`. */
	const GAP = 4;

	let innerEl = $state<HTMLElement | undefined>(undefined);
	let measureEl = $state<HTMLElement | undefined>(undefined);
	let moreEl = $state<HTMLElement | undefined>(undefined);
	let moreBtn = $state<HTMLButtonElement | undefined>(undefined);
	let availW = $state(0);
	let widths = $state<number[]>([]);
	let moreW = $state(0);
	let open = $state(false);

	// Re-measure when a label's TEXT changes, not just when the tab list does —
	// switching locale keeps the same hrefs but can change every width.
	let labelSig = $derived(tabs.map((t) => m(t.labelKey)).join('\u0000'));

	/**
	 * Which tabs fit on one row, and which fall into the More menu.
	 *
	 * Before the first measurement (`widths` empty, or stale after the tab list
	 * changed) everything renders inline; `.section-tabs-row` is `overflow:
	 * hidden` on its cross axis and scrolls on its main one, so that pre-measure
	 * frame scrolls inside the row instead of widening the document.
	 */
	/**
	 * True once the off-screen row has been measured for THIS tab set, so the
	 * split below is real rather than the pre-measure "render everything" pass.
	 * Exposed as `data-tabs-ready` because it is the only honest signal that the
	 * bar has settled: until it flips, a tab may be in the row one frame and in
	 * the More menu the next.
	 */
	let measured = $derived(widths.length === tabs.length && availW > 0);

	let split = $derived.by((): { visible: NavChild[]; overflow: NavChild[] } => {
		if (!measured) {
			return { visible: tabs, overflow: [] };
		}
		const { visible, overflow } = splitSectionTabs({
			widths,
			barWidth: availW,
			moreWidth: moreW,
			gap: GAP,
			activeIndex
		});
		return { visible: visible.map((i) => tabs[i]), overflow: overflow.map((i) => tabs[i]) };
	});

	// The budget is the content width of `.section-tabs-inner`, which holds the
	// row AND the More button — not the row's own width, which has already
	// shrunk by More's once More renders (`lib/sectionTabsSplit.ts`).
	$effect(() => {
		const el = innerEl;
		if (!el) return;
		const contentWidth = () => {
			const cs = getComputedStyle(el);
			return el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
		};
		const ro = new ResizeObserver(() => (availW = contentWidth()));
		ro.observe(el);
		availW = contentWidth();
		return () => ro.disconnect();
	});

	$effect(() => {
		// `labelSig` is read so a locale switch re-measures (see above).
		void labelSig;
		const el = measureEl;
		if (!el) return;
		widths = Array.from(el.querySelectorAll<HTMLElement>('[data-measure-tab]')).map(
			(n) => n.getBoundingClientRect().width
		);
		moreW = el.querySelector<HTMLElement>('[data-measure-more]')?.getBoundingClientRect().width ?? 0;
	});

	// Navigating from inside the menu should leave it closed.
	$effect(() => {
		void page.url.href;
		open = false;
	});

	$effect(() => {
		if (!open) return;
		const onPointerDown = (e: PointerEvent) => {
			if (!moreEl?.contains(e.target as Node)) open = false;
		};
		const onKeyDown = (e: KeyboardEvent) => {
			if (e.key === 'Escape') {
				open = false;
				moreBtn?.focus();
			}
		};
		document.addEventListener('pointerdown', onPointerDown);
		document.addEventListener('keydown', onKeyDown);
		return () => {
			document.removeEventListener('pointerdown', onPointerDown);
			document.removeEventListener('keydown', onKeyDown);
		};
	});
</script>

<!-- Only show the bar when the section actually offers a choice. A lone
     accessible tab (e.g. a CFO's Settings = just Organization) would just
     duplicate the page title, so we suppress it. -->
{#if group && tabs.length > 1}
	<nav
		class="section-tabs"
		aria-label={m('shell.sectionNav', { group: m(group.labelKey) })}
		data-tabs-ready={measured ? 'true' : 'false'}
	>
		<div class="section-tabs-inner" bind:this={innerEl}>
			<div class="section-tabs-row">
				{#each split.visible as tab (tab.href)}
					{@const active = sectionTabActive(tab, tabs, page.url)}
					<a
						href={tab.href}
						class="section-tab"
						class:active
						aria-current={active ? 'page' : undefined}
					>
						{m(tab.labelKey)}
					</a>
				{/each}
			</div>

			{#if split.overflow.length > 0}
				<div class="section-more" bind:this={moreEl}>
					<button
						type="button"
						class="section-tab section-more-btn"
						bind:this={moreBtn}
						aria-expanded={open}
						aria-haspopup="true"
						aria-controls="section-more-menu"
						aria-label={m('shell.sectionMoreAria', { count: String(split.overflow.length) })}
						onclick={() => (open = !open)}
					>
						{m('shell.sectionMore')}
						<svg
							width="12"
							height="12"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2.5"
							aria-hidden="true"
						>
							<polyline points="6 9 12 15 18 9" />
						</svg>
					</button>
					{#if open}
						<div class="section-more-menu" id="section-more-menu">
							{#each split.overflow as tab (tab.href)}
								{@const active = sectionTabActive(tab, tabs, page.url)}
								<a
									href={tab.href}
									class="section-more-item"
									class:active
									aria-current={active ? 'page' : undefined}
								>
									{m(tab.labelKey)}
								</a>
							{/each}
						</div>
					{/if}
				</div>
			{/if}
		</div>

		<!--
			Off-screen copy used only to read each tab's intrinsic width, so the
			split can be computed without ever rendering an over-wide row. Spans,
			not anchors: focusable content inside `aria-hidden` is reachable by
			keyboard while hidden from assistive tech, which is its own defect.
		-->
		<div class="section-tabs-measure-clip">
			<div class="section-tabs-measure" bind:this={measureEl} aria-hidden="true">
				{#each tabs as tab (tab.href)}
					<span class="section-tab" data-measure-tab>{m(tab.labelKey)}</span>
				{/each}
				<span class="section-tab section-more-btn" data-measure-more>
					{m('shell.sectionMore')}
					<svg width="12" height="12" viewBox="0 0 24 24" aria-hidden="true"></svg>
				</span>
			</div>
		</div>
	</nav>
{/if}

<style>
	/* Secondary nav strip above the page content. Full-width border, inner
	   content aligned to the same 1800px / 20px gutter as `.workspace` so the
	   tabs line up with the page title below. */
	/* Not sticky, on purpose. A pinned strip covers whatever the browser (or
	   Playwright) scrolls into view just beneath it — an intercepted click in
	   every grouped page's specs, and a focused control hidden under it
	   (WCAG 2.4.11), the defect the marketing header needed scroll padding for. */
	.section-tabs {
		border-bottom: 1px solid var(--border);
		/* Opaque --surface for the same reason as the sidebar: the tab labels'
		   contrast is calibrated on it, and a translucent strip would hand that
		   decision to whatever scrolls underneath. */
		background: var(--surface);
		position: relative;
		box-shadow: 0 10px 24px -20px rgba(0, 0, 0, 0.9);
	}

	.section-tabs-inner {
		max-width: 1800px;
		margin: 0 auto;
		padding: 0 28px;
		display: flex;
		align-items: stretch;
	}

	/* `min-width: 0` lets this flex child shrink below its content width, so
	   the row can never widen the document into a horizontal scrollbar (WCAG
	   1.4.10 — the whole page used to scroll sideways here). The More menu
	   takes every tab that does not fit, but it always leaves at least one —
	   the active one — in the row, and at the 320px floor a single long label
	   (a German one, or a section name beside the More button) can still be
	   wider than what is left. The row SCROLLS sideways inside itself then,
	   rather than clipping the label it cannot fit (decisions §590): every
	   item in it is a link, so the region is keyboard-reachable and focusing a
	   tab scrolls it into view. `overflow-y: hidden` keeps the scroll on one
	   axis; the active indicator sits inside the tab (`bottom: 0`), so nothing
	   is hung below the row for it to clip. */
	.section-tabs-row {
		display: flex;
		gap: 4px;
		min-width: 0;
		flex: 1;
		overflow-x: auto;
		overflow-y: hidden;
		scrollbar-width: thin;
		overscroll-behavior-x: contain;
	}

	.section-tab {
		position: relative;
		flex: none;
		padding: 13px 14px;
		color: var(--text-muted);
		font-size: 0.88rem;
		font-weight: 500;
		text-decoration: none;
		white-space: nowrap;
		transition: color 0.15s;
	}
	/* The indicator is a pseudo-element that grows from the centre, so the
	   active tab reads as selected at a glance and the change of tab has a
	   moment of motion; it never moves layout. It sits at `bottom: 0`, inside the
	   tab, because `.section-tabs-row` clips its cross axis (`overflow-y: hidden`) and would clip
	   anything hung below it over the strip's border. */
	.section-tab::after {
		content: '';
		position: absolute;
		left: 14px;
		right: 14px;
		bottom: 0;
		height: 2px;
		border-radius: 2px 2px 0 0;
		background: var(--accent);
		transform: scaleX(0);
		transition: transform 0.25s var(--ease-out);
	}

	.section-tab:hover {
		color: var(--text);
	}

	.section-tab.active {
		color: var(--text);
	}
	.section-tab.active::after {
		transform: scaleX(1);
		box-shadow: 0 0 10px var(--accent-glow);
	}

	@media (max-width: 768px) {
		.section-tabs-inner {
			padding: 0 14px;
		}
	}

	.section-more {
		position: relative;
		flex: none;
	}

	.section-more-btn {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		background: none;
		border: none;
		font-family: inherit;
		cursor: pointer;
	}

	.section-more-menu {
		position: absolute;
		top: 100%;
		right: 0;
		z-index: 40;
		min-width: 200px;
		padding: 6px;
		display: flex;
		flex-direction: column;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: 0 8px 24px rgb(0 0 0 / 0.25);
	}

	.section-more-item {
		padding: 8px 12px;
		border-radius: 6px;
		color: var(--text-muted);
		font-size: 0.9rem;
		font-weight: 500;
		text-decoration: none;
		white-space: nowrap;
	}

	.section-more-item:hover {
		/* Same value as `Sidebar.nav-item:hover` / `NotificationBell.bell-item:hover`
		   — this is a nav row in a popover, so it matches the chrome it belongs
		   to rather than inventing a token. `--surface-hover` does not exist;
		   using it with a fallback is a dead token and `a11y/tokenPairing` says
		   so. */
		background: color-mix(in srgb, var(--accent) 8%, transparent);
		color: var(--text);
	}

	.section-more-item.active {
		color: var(--accent);
	}

	/* Zero-height, clipping wrapper. The measurement row is deliberately WIDER
	   than the viewport, and `position: absolute` + `visibility: hidden` does
	   NOT take an element out of the document's scrollable area — so without
	   this the measurer reintroduced the very horizontal scrollbar the
	   component exists to remove (372px of it at a 1000px viewport). Clipping
	   affects painting, not layout, so the children still report real widths. */
	.section-tabs-measure-clip {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
		height: 0;
		overflow: hidden;
	}

	/* Measured, never seen. `visibility: hidden` (not `display: none`) so the
	   browser still lays it out and reports real widths. */
	.section-tabs-measure {
		position: absolute;
		top: 0;
		left: 0;
		display: flex;
		gap: 4px;
		visibility: hidden;
		pointer-events: none;
		white-space: nowrap;
	}
</style>
