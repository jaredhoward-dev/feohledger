<script lang="ts">
	import { onDestroy, untrack } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { theme } from '#lib/stores/theme.svelte.ts';
	import type { ResolvedTheme } from '#lib/theme.ts';

	/**
	 * The hCaptcha widget for the public forms that send email to whatever
	 * address is typed: signup and "email me my workspace address" (decisions
	 * §518). One owner, so the blocked-script handling and the theme following
	 * below are not written twice.
	 *
	 * Renders nothing when `sitekey` is empty (the local-first default: the
	 * backend then skips verification too). The parent binds `token` and
	 * `loadFailed`, and calls `reset()` after every answer from the server.
	 */
	interface Props {
		/** `hcaptcha_sitekey` from `/api/public-config`; empty = no widget. */
		sitekey: string;
		/** The solved token, or `null` while unsolved / expired / spent. */
		token?: string | null;
		/** hCaptcha's script could not load — almost always a content blocker. */
		loadFailed?: boolean;
		/** Id of the blocked notice, for the submit button's `aria-describedby`. */
		blockedId?: string;
	}

	let {
		sitekey,
		token = $bindable(null),
		loadFailed = $bindable(false),
		blockedId = 'captcha-blocked'
	}: Props = $props();

	// hCaptcha's API has loaded (its explicit-mode onload fired).
	let apiReady = $state(false);
	let el = $state<HTMLDivElement | null>(null);
	// The widget currently drawn: which container, and in which palette. Plain,
	// not $state — only the effect reads it, and it must not re-run that effect
	// by being written there.
	let widget: { id: string; el: HTMLDivElement; theme: ResolvedTheme } | null = null;
	const ONLOAD = 'feohHCaptchaOnLoad';

	// Load once a sitekey is known — at mount, or when the parent's config
	// fetch resolves after it.
	$effect(() => {
		if (sitekey && !apiReady && !loadFailed) untrack(load);
	});

	/**
	 * Load hCaptcha in EXPLICIT mode: it draws nothing by itself, and calls
	 * `ONLOAD` when its API is ready. Every widget is then drawn by the effect
	 * below, which is the only way to give it a theme after the fact —
	 * hCaptcha has no call to change a live widget's theme, so following the
	 * page means removing the widget and drawing it again.
	 *
	 * **When the script cannot load** — a content blocker, a browser privacy
	 * shield, a filtering proxy — the form used to say only "Please complete
	 * the captcha" with nothing on screen to complete. The `<script>` element's
	 * own events are the signal, not a timer: `error` when the request fails,
	 * and `load` with no `hcaptcha` global when a blocker answered it with an
	 * empty stand-in. Either way the failed element is removed, so the next
	 * visit asks again (the visitor may have allowed the page in their blocker
	 * meanwhile) instead of finding an element that looks like it is still
	 * loading. The listeners go on whichever element is in the head — this
	 * visit's, or one an earlier visit added that is still in flight.
	 */
	function load() {
		const w = window as any;
		w[ONLOAD] = () => (apiReady = true);
		// Already loaded by an earlier visit in the same session.
		if (typeof w.hcaptcha?.render === 'function') {
			apiReady = true;
			return;
		}
		let s = document.querySelector<HTMLScriptElement>('script[data-hcaptcha]');
		if (!s) {
			s = document.createElement('script');
			s.src = `https://hcaptcha.com/1/api.js?render=explicit&onload=${ONLOAD}`;
			s.async = true;
			s.defer = true;
			s.setAttribute('data-hcaptcha', 'true');
			document.head.appendChild(s);
		}
		const script = s;
		const failed = () => {
			script.remove();
			loadFailed = true;
		};
		script.addEventListener('error', failed, { once: true });
		// hCaptcha's api.js defines its `hcaptcha` global synchronously as it
		// runs, so by `load` it exists; readiness itself is still the `onload`
		// callback above. Only a script that ran and defined NOTHING — a
		// blocker's empty stand-in — counts as failed here.
		script.addEventListener(
			'load',
			() => {
				if (!w.hcaptcha) failed();
			},
			{ once: true }
		);
	}

	/**
	 * Keep exactly one widget in the form, drawn in the palette the page is
	 * showing (docs/decisions.md §289, §290).
	 *
	 * The widget is an iframe and cannot see our palette, so it is told which
	 * one is showing. It re-runs when the container is mounted (first paint,
	 * and the form coming back from a confirmation screen), when the API
	 * finishes loading, and when `theme.resolved` changes.
	 *
	 * A SOLVED widget is left alone on a theme change: redrawing it would throw
	 * away a token the visitor already earned, for a cosmetic reason. It is
	 * redrawn in the current palette once that token is gone — expired
	 * (hCaptcha resets the widget then anyway) or spent on a submission.
	 */
	$effect(() => {
		const container = el;
		const wanted = theme.resolved;
		const solved = token !== null;
		if (!sitekey || !apiReady || !container) return;
		untrack(() => sync(container, wanted, solved));
	});

	function sync(container: HTMLDivElement, wanted: ResolvedTheme, solved: boolean) {
		const hcaptcha = (window as any).hcaptcha;
		if (typeof hcaptcha?.render !== 'function') return;
		if (widget) {
			const sameContainer = widget.el === container;
			if (sameContainer && (widget.theme === wanted || solved)) return;
			try {
				hcaptcha.remove(widget.id);
			} catch {
				// Its container left the DOM with the form; nothing to remove.
			}
			widget = null;
		}
		const id = hcaptcha.render(container, {
			sitekey,
			theme: wanted,
			callback: (t: string) => (token = t),
			'expired-callback': () => (token = null)
		});
		widget = { id, el: container, theme: wanted };
	}

	// The widget's container leaves with this component (the form giving way
	// to a confirmation screen): remove the widget with it, so the next form
	// draws a fresh one rather than hCaptcha holding a detached one.
	onDestroy(() => {
		if (!widget) return;
		try {
			(window as any).hcaptcha?.remove(widget.id);
		} catch {
			// Already gone.
		}
		widget = null;
	});

	/**
	 * Throw away a token the server may already have spent.
	 *
	 * The backend verifies the captcha before the rest of the request, so a
	 * refusal arrives with the token used up while the widget still shows a
	 * tick. Kept, it satisfied the client-side check and rode along on the
	 * retry, which the server refused with "Captcha verification failed" — a
	 * misleading reason, with nothing on screen to solve again. Call it on
	 * every answer from the server; the widget is reset so it looks as
	 * unsolved as it now is.
	 */
	export function reset() {
		if (token === null) return;
		token = null;
		const hcaptcha = (window as any).hcaptcha;
		if (widget && typeof hcaptcha?.reset === 'function') {
			try {
				hcaptcha.reset(widget.id);
			} catch {
				// Its container already left the DOM; the effect draws a new one.
			}
		}
	}
</script>

{#if sitekey}
	{#if loadFailed}
		<!-- Where the widget would have been, and announced on arrival: without
		     it the only thing on screen was a "complete the captcha" request
		     with nothing to complete. -->
		<div class="error captcha-blocked" id={blockedId} role="alert">
			<strong>{m('auth.signup.captchaBlockedTitle')}</strong>
			<span>{m('auth.signup.captchaBlockedBody')}</span>
		</div>
	{:else}
		<!-- Drawn into by the effect above (explicit render), never by hCaptcha
		     scanning for the class. -->
		<div class="h-captcha" bind:this={el}></div>
	{/if}
{/if}
