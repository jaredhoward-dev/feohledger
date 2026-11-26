import type { Action } from 'svelte/action';

import { syncModalInert } from './modalInert.ts';

interface FocusTrapParams {
	/** Invoked on Escape pressed while focus is within the trapped element. */
	onEscape?: () => void;
}

const FOCUSABLE =
	'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Marks a subtree that is hosted INSIDE a dialog without being part of its
 * content — today only the toast region (`ui/Toast.svelte`), which moves into
 * the open modal dialog so its live regions and dismiss buttons are not hidden
 * by `aria-modal` (WCAG 2.1.1, 4.1.3). Its controls join the Tab cycle AFTER
 * the dialog's own, never take the dialog's initial focus, and never count as
 * the dialog's action row.
 */
export const DIALOG_SATELLITE_ATTR = 'data-dialog-satellite';

/**
 * Where Tab / Shift+Tab goes at a boundary of the cycle
 * `own[0] … own[n-1] → satellite[0] … satellite[m-1] → own[0]`, or null to let
 * the browser move focus natively (anywhere that is not a boundary, so radio
 * groups and composite widgets keep their own Tab behaviour). `active` is null
 * when focus is on the dialog node itself or has strayed outside it: the cycle
 * is re-entered at its start going forward and at its end going back. A
 * focused element inside the dialog that is in neither list (a `tabindex="-1"`
 * heading) moves natively. Pure, so it unit-tests without a DOM
 * (`focusTrap.test.ts`).
 */
export function tabBoundaryTarget<T>(
	own: T[],
	satellites: T[],
	active: T | null,
	shift: boolean
): T | null {
	const cycle = [...own, ...satellites];
	if (cycle.length === 0) return null;
	if (active === null) return shift ? cycle[cycle.length - 1] : cycle[0];
	const at = cycle.indexOf(active);
	if (at === -1) return null;
	const boundary = shift
		? at === 0 || at === own.length // own[0] → end; first satellite → last own
		: at === cycle.length - 1 || at === own.length - 1; // last of all → start; last own → first satellite
	if (!boundary) return null;
	return cycle[(at + (shift ? -1 : 1) + cycle.length) % cycle.length];
}

/**
 * The open modal dialogs (those carrying `aria-modal="true"`), oldest first.
 * The toast region hosts itself in the topmost one (`ui/Toast.svelte`), and
 * everything outside the topmost one is made inert (`./modalInert.ts`); this
 * is the deterministic signal for both — mounted/unmounted here, not inferred
 * from DOM mutations.
 */
const openModals: HTMLElement[] = [];
const modalListeners = new Set<() => void>();

/** The topmost open modal dialog, or null. */
export function topModalDialog(): HTMLElement | null {
	return openModals.at(-1) ?? null;
}

/** Calls `fn` whenever a modal dialog opens or closes; returns the unsubscribe. */
export function onModalDialogsChange(fn: () => void): () => void {
	modalListeners.add(fn);
	return () => modalListeners.delete(fn);
}

function notifyModals() {
	// Everything outside the topmost dialog is inert (`./modalInert.ts`,
	// decisions §384) — synchronously, before any listener runs and before the
	// closing dialog's deferred focus restore, so the trigger it returns to is
	// focusable again by then.
	syncModalInert(topModalDialog());
	for (const fn of [...modalListeners]) fn();
}

/**
 * Traps keyboard focus inside `node` while it is mounted, then restores focus to
 * the element that was focused before it mounted. Implements WCAG 2.1.2 (No
 * Keyboard Trap — Tab / Shift+Tab wrap within the dialog instead of escaping to
 * the page behind it) and 2.4.3 (Focus Order — focus returns to the trigger on
 * close). The node should carry `tabindex="-1"` so it can hold focus itself when
 * it has no focusable descendants.
 *
 * This is the single shared implementation, and `ui/Modal.svelte` is its only
 * caller: every modal dialog in the app — `InvoiceModal` and `RunDetailModal`
 * included, and the supplier portal's discount-accept dialog — is a `Modal`,
 * so every dialog gets identical focus management. `src/lib/modalShell.test.ts`
 * fails on a hand-rolled dialog shell outside `Modal`.
 */
/**
 * Where focus goes when a dialog closes (WCAG 2.4.3 Focus Order).
 *
 * 1. Only when focus was actually LOST — sitting on `<body>`, which is where the
 *    browser puts it when the focused dialog node is removed. If something else
 *    already holds focus (a second dialog opened in the same flush, or a caller
 *    that moved focus deliberately), restoring would steal it back.
 * 2. To the trigger, if it is still in the document, is not `<body>` (nothing
 *    was focused, as when a deep link opened the dialog) and accepts focus — a
 *    disabled button (a row whose action is now in flight) silently refuses
 *    `focus()`, which is why the check reads `activeElement` afterwards rather
 *    than trusting the call.
 * 3. Otherwise to the page's `<main id="main-content" tabindex="-1">` — the
 *    landmark both shells (`routes/+layout.svelte`, `routes/portal/+layout.svelte`)
 *    already expose as the skip-link target, so the user resumes inside the
 *    content they were working in instead of at the top of the document.
 *
 * Guard: `tests-e2e/a11y/screen-reader.spec.ts` (both the surviving-trigger and
 * the vanished-trigger cases).
 */
/**
 * Does Escape belong to `node`? A registered modal answers it only while it is
 * the topmost; an unregistered trap (not `aria-modal`) always does. Pure, so
 * the rule is unit-tested without a DOM.
 */
export function ownsEscape<T>(stack: readonly T[], node: T): boolean {
	return !stack.includes(node) || stack.at(-1) === node;
}

/**
 * Does focus already sit where the user meant it, so a closing dialog must not
 * restore? Not when nothing holds it (`<body>`), and not when an open dialog's
 * own node holds it while containing the trigger: closing a dialog stacked
 * inside another drops focus to `<body>` for a moment, and the host's
 * focus-loss handler (`onFocusOut`) can reclaim it onto the host node before
 * this restore runs. That node is a fallback, not a choice; the trigger inside
 * it is where focus belongs (WCAG 2.4.3). Pure, so it is unit-tested.
 */
export function focusIsSettled<T extends { contains(other: T): boolean }>(
	active: T | null,
	body: T,
	openDialogs: readonly T[],
	trigger: T | null
): boolean {
	if (!active || active === body) return false;
	if (trigger && openDialogs.includes(active) && active !== trigger && active.contains(trigger)) {
		return false;
	}
	return true;
}

function restoreFocus(prev: HTMLElement | null): void {
	const active = document.activeElement as HTMLElement | null;
	if (active?.isConnected && focusIsSettled(active, document.body, openModals, prev)) return;
	// `<body>` is no trigger: a dialog opened from a deep link (`?id=`) had
	// nothing focused, and "restoring" body is the top-of-document drop rule 3
	// exists to prevent.
	if (prev?.isConnected && prev !== document.body) {
		prev.focus?.();
		if (document.activeElement === prev) return;
	}
	document.getElementById('main-content')?.focus();
}

export const focusTrap: Action<HTMLElement, FocusTrapParams | undefined> = (node, params) => {
	let onEscape = params?.onEscape;
	// Where focus was before the dialog opened — restored on destroy (2.4.3).
	const prevFocused = (document.activeElement as HTMLElement) ?? null;

	// Visible, focusable descendants in DOM order, the dialog's own first and
	// any satellite's (the hosted toast region) after them.
	function focusable(): { own: HTMLElement[]; satellites: HTMLElement[] } {
		const own: HTMLElement[] = [];
		const satellites: HTMLElement[] = [];
		for (const el of node.querySelectorAll<HTMLElement>(FOCUSABLE)) {
			if (!(el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement)) continue;
			(el.closest(`[${DIALOG_SATELLITE_ATTR}]`) ? satellites : own).push(el);
		}
		return { own, satellites };
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			// A dialog stacked inside another (a confirm a panel opens over its
			// host) shares the host's DOM, so its Escape bubbles here too: only
			// the topmost dialog closes, never the one under it as well.
			if (!ownsEscape(openModals, node)) return;
			// Handled: stop it here. Closing this dialog can unmount it (and
			// pop it off the stack) before the same event bubbles on to the
			// host's trap, which would then find itself topmost and close too.
			e.stopPropagation();
			onEscape?.();
			return;
		}
		if (e.key !== 'Tab') return;
		const { own, satellites } = focusable();
		if (own.length + satellites.length === 0) {
			// Nothing tabbable but the dialog itself — hold focus on it.
			e.preventDefault();
			node.focus();
			return;
		}
		const active = document.activeElement as HTMLElement | null;
		const inside = active && active !== node && node.contains(active) ? active : null;
		const target = tabBoundaryTarget(own, satellites, inside, e.shiftKey);
		if (target) {
			e.preventDefault();
			target.focus();
		}
	}

	// Move focus into the dialog once its content is in the DOM (next microtask).
	// A dialog whose first control is NOT the safe default (a confirm laid out
	// "Leave · Stay") marks the one that is with `data-autofocus`.
	// Never a satellite: a toast already showing is not where the dialog starts.
	queueMicrotask(() => {
		const { own } = focusable();
		(own.find((el) => el.hasAttribute('data-autofocus')) ?? own[0] ?? node).focus();
	});
	node.addEventListener('keydown', onKey);

	/**
	 * Focus dropped out of the open dialog to `<body>` — the focused control was
	 * disabled under it (Save turns into a disabled "Saving…" while the request
	 * runs) or removed. Nothing else would bring it back: the Tab handler above
	 * listens on the dialog, so the next Tab went to the top of the page behind
	 * it, out of a modal (2.1.2, 2.4.3). Focus goes to the dialog node itself
	 * (`tabindex="-1"`), from where Tab resumes at its first control. A
	 * satellite's own loss (a dismissed toast) is left to `ui/Toast.svelte`,
	 * which hands focus back to where it came from.
	 */
	function onFocusOut(e: FocusEvent) {
		if (e.relatedTarget) return;
		if ((e.target as Element | null)?.closest?.(`[${DIALOG_SATELLITE_ATTR}]`)) return;
		// Only the topmost dialog reclaims focus: one with another stacked over
		// it was made inert, which drops its focused control, and must not pull
		// focus back from the dialog that now owns it.
		queueMicrotask(() => {
			if (
				node.isConnected &&
				(!openModals.includes(node) || openModals.at(-1) === node) &&
				(document.activeElement === document.body || !document.activeElement)
			) {
				node.focus();
			}
		});
	}
	node.addEventListener('focusout', onFocusOut);

	// The same loss where the browser fires no `focusout` (focus fix-up when
	// the focused control is disabled): a Tab pressed with focus on `<body>`
	// while this is the topmost modal re-enters the dialog's cycle.
	function onDocumentKey(e: KeyboardEvent) {
		if (e.key !== 'Tab' || openModals.at(-1) !== node) return;
		const active = document.activeElement;
		if (active && active !== document.body) return;
		const { own, satellites } = focusable();
		e.preventDefault();
		(tabBoundaryTarget(own, satellites, null, e.shiftKey) ?? node).focus();
	}
	document.addEventListener('keydown', onDocumentKey);

	// Only a real modal registers: the toast region is hosted inside it.
	const modal = node.getAttribute('aria-modal') === 'true';
	if (modal) {
		openModals.push(node);
		notifyModals();
	}

	return {
		update(p?: FocusTrapParams) {
			onEscape = p?.onEscape;
		},
		destroy() {
			node.removeEventListener('keydown', onKey);
			node.removeEventListener('focusout', onFocusOut);
			document.removeEventListener('keydown', onDocumentKey);
			if (modal) {
				const i = openModals.indexOf(node);
				if (i !== -1) openModals.splice(i, 1);
				notifyModals();
			}
			// Deferred to a microtask, not done here. The action is torn down in
			// the SAME flush as whatever the dialog's action changed, so the
			// trigger may be about to disappear: approving a pending change
			// request from its dialog filters the row (and the button that opened
			// the dialog) out of the Pending list. Focusing it synchronously
			// "succeeds" and is then lost to `<body>` when the row unmounts a
			// moment later — a keyboard or screen-reader user is thrown back to
			// the top of the document (WCAG 2.4.3). After the flush the DOM is
			// settled, so the restore can see what really survived.
			queueMicrotask(() => restoreFocus(prevFocused));
		},
	};
};
