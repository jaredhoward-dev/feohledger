/**
 * The app-shell sidebar's state.
 *
 * Two separate user choices, one per viewport class, so neither overwrites the
 * other:
 *
 * - On a WIDE viewport the reader picks the 60px icon rail or the 220px panel
 *   (`toggle()`), and that choice is theirs to keep.
 * - On a NARROW one (at or below the scale's `lg` step, `lib/breakpoints.ts`;
 *   `narrow` is set by the root layout's `matchMedia` listener) the shell is
 *   always the icon rail unless the reader opens the panel. An opened panel is
 *   an OVERLAY — it floats over the page behind a backdrop instead of pushing
 *   the content aside, because at the 320px floor a pushed page is left ~100px
 *   wide (#635 finding 39). Crossing into narrow closes it again.
 *
 * Crossing back to wide restores the reader's wide choice rather than
 * whatever the narrow viewport forced, which is what "auto-collapse respects
 * an explicit choice" means here (decisions §590). `collapsed` is the derived
 * answer the shell renders; `overlay` is the one place the shell asks whether
 * the panel is floating.
 */
function createSidebarStore() {
	let wideCollapsed = $state(false);
	let narrowOpen = $state(false);
	let narrow = $state(false);

	return {
		/** The icon rail is showing (instead of the 220px panel). */
		get collapsed() {
			return narrow ? !narrowOpen : wideCollapsed;
		},
		get narrow() {
			return narrow;
		},
		/** The expanded panel is floating over the page (narrow viewport only). */
		get overlay() {
			return narrow && narrowOpen;
		},
		/** The reader's own expand / collapse, applied to the current viewport class. */
		toggle() {
			if (narrow) narrowOpen = !narrowOpen;
			else wideCollapsed = !wideCollapsed;
		},
		/** Collapse to the icon rail (closes the overlay on a narrow viewport). */
		collapse() {
			if (narrow) narrowOpen = false;
			else wideCollapsed = true;
		},
		/** Called by the root layout when the viewport crosses the narrow breakpoint. */
		setNarrow(value: boolean) {
			if (value === narrow) return;
			narrow = value;
			narrowOpen = false;
		}
	};
}

export const sidebar = createSidebarStore();
