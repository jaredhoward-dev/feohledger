<script lang="ts">
	/**
	 * Organization → SCIM provisioning (decisions §510).
	 *
	 * Generates, rotates and revokes the tenant's SCIM bearer token through
	 * `POST` / `DELETE /api/organization/sso/scim-token`, both admin-only and
	 * audited first. The backend stores only the token's sha256 digest, so the
	 * panel can only ever say WHETHER a token is set
	 * (`GET /api/organization/sso` → `scim_token_configured`).
	 *
	 * The plaintext comes back once, from the mint. It goes straight into
	 * `SecretReveal` and is dropped from state when that dialog closes: never
	 * stored, logged or put in a URL. Rotating and revoking each ask first,
	 * because both cut off the identity provider's current connection.
	 *
	 * Generating or rotating demands a second factor (decisions §510): the
	 * server answers `sensitive_step_up_required` until a code or passkey proof
	 * is sent, and the panel collects one in `ui/StepUpPrompt` and resends —
	 * the same flow as `CloseWorkspacePanel` (§306). Revoking needs none.
	 *
	 * Focus: Generate and Revoke each replace the button that was pressed, so
	 * when their dialog closes focus goes to the panel's new primary action
	 * (Rotate after a mint, Generate after a revoke) instead of falling back
	 * to the page landmark (WCAG 2.4.3).
	 *
	 * Admin-only end to end: `readOnly` renders the notice and makes no read.
	 */
	import { tick } from 'svelte';
	import LinkedMessage from '#lib/components/ui/LinkedMessage.svelte';
	import StepUpPrompt from '#lib/components/ui/StepUpPrompt.svelte';
	import OrgSettingPanel from '#lib/components/organization/OrgSettingPanel.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import PlanUpgradeNotice from '#lib/components/ui/PlanUpgradeNotice.svelte';
	import SecretReveal from '#lib/components/ui/SecretReveal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { ApiError } from '#lib/api.ts';
	import { authErrorMessage } from '#lib/api/authRefusals.ts';
	import { getSsoSettings, mintScimToken, revokeScimToken } from '#lib/api/ssoSettings.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { auth, type StepUpProof } from '#lib/stores/auth.svelte.ts';
	import { getApiBase } from '#lib/tenant.ts';
	import { FEATURE_SCIM } from '#lib/types/planFeatures.ts';
	import type { ScimTokenMinted } from '#lib/types/ssoSettings.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';
	import { scimActions, scimBaseUrl } from '#lib/utils/scim.ts';

	let {
		readOnly,
		userLoaded
	}: {
		/** The caller is not an admin: render the notice, read nothing. */
		readOnly: boolean;
		/** `/auth/me` has resolved, so `readOnly` is final. */
		userLoaded: boolean;
	} = $props();

	const uid = $props.id();

	let configured = $state<boolean | null>(null);
	let loading = $state(false);
	let loadFailed = $state(false);
	let working = $state(false);
	let confirming = $state<'rotate' | 'revoke' | null>(null);
	/** The one-time plaintext. Lives only while the reveal dialog is open. */
	let minted = $state<ScimTokenMinted | null>(null);
	let requested = false;
	let stepUpOpen = $state(false);
	let stepUpError = $state<string | null>(null);
	let noFactor = $state(false);
	let generateButton = $state<HTMLButtonElement | null>(null);
	let rotateButton = $state<HTMLButtonElement | null>(null);

	const entitled = $derived(auth.hasFeature(FEATURE_SCIM));
	const actions = $derived(configured === null ? [] : scimActions(configured, entitled));
	const endpoint = $derived(
		scimBaseUrl(getApiBase(), typeof window === 'undefined' ? '' : window.location.origin)
	);

	async function load() {
		loading = true;
		loadFailed = false;
		try {
			configured = (await getSsoSettings()).scim_token_configured;
		} catch {
			loadFailed = true;
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		if (requested || !userLoaded || readOnly) return;
		requested = true;
		void load();
	});

	/** Move focus to whichever primary action the panel now shows, once the
	 *  DOM has settled (the pressed button may just have been replaced). */
	async function focusPrimary() {
		await tick();
		(configured ? rotateButton : generateButton)?.focus();
	}

	async function generate(stepUp?: StepUpProof) {
		confirming = null;
		working = true;
		noFactor = false;
		try {
			minted = await mintScimToken(stepUp);
			stepUpOpen = false;
			stepUpError = null;
			configured = true;
			toast(m('org.scim.generated'), 'success');
		} catch (err) {
			const code = err instanceof ApiError ? err.code : null;
			if (code === 'sensitive_step_up_required') {
				stepUpError = null;
				stepUpOpen = true;
			} else if (code === 'sensitive_step_up_failed' && stepUpOpen) {
				stepUpError = authErrorMessage(err, m, 'stepUpPrompt.failed');
			} else if (code === 'sensitive_step_up_no_factor') {
				stepUpOpen = false;
				noFactor = true;
			} else {
				stepUpOpen = false;
				toast(errorMessage(err, m('org.scim.actionFailed')), 'error');
			}
		} finally {
			working = false;
		}
	}

	async function revoke() {
		confirming = null;
		working = true;
		try {
			await revokeScimToken();
			configured = false;
			toast(m('org.scim.revoked'), 'success');
			void focusPrimary();
		} catch (err) {
			toast(errorMessage(err, m('org.scim.actionFailed')), 'error');
		} finally {
			working = false;
		}
	}

	function dismissMinted() {
		minted = null;
		void focusPrimary();
	}
</script>

<OrgSettingPanel
	title={m('org.section.scim')}
	intro={m('org.scim.intro')}
	testId="scim"
	{readOnly}
	loading={loading || (configured === null && !loadFailed)}
	{loadFailed}
	loadFailedMessage={m('org.scim.loadFailed')}
	onretry={load}
>
	<p class="status" data-testid="scim-status" data-configured={configured ? 'true' : 'false'}>
		{configured ? m('org.scim.status.set') : m('org.scim.status.unset')}
	</p>
	<div class="endpoint">
		<label for="{uid}-endpoint">{m('org.scim.endpointLabel')}</label>
		<input
			id="{uid}-endpoint"
			type="text"
			readonly
			value={endpoint}
			aria-describedby="{uid}-endpoint-hint"
			data-testid="scim-endpoint"
		/>
		<p id="{uid}-endpoint-hint" class="field-hint">{m('org.scim.endpointHint')}</p>
	</div>
	{#if noFactor}
		<p class="no-factor" role="alert" data-testid="scim-no-factor">
			<LinkedMessage
				text={m('org.scim.noFactor')}
				links={{
					profile: { href: '/profile?section=mfa', label: m('org.scim.noFactorLink') }
				}}
			/>
		</p>
	{/if}
	{#if userLoaded && !entitled}
		<PlanUpgradeNotice feature={FEATURE_SCIM} testId="scim-plan-upgrade" />
	{/if}
	<p class="help-link">
		<a href="/help/guides/configure-organization">{m('org.scim.helpLink')}</a>
	</p>
	{#snippet footer()}
		{#if actions.includes('generate')}
			<button
				type="button"
				class="btn-primary"
				disabled={working}
				bind:this={generateButton}
				onclick={() => generate()}
				data-testid="scim-generate"
			>
				{working ? m('org.scim.working') : m('org.scim.generate')}
			</button>
		{/if}
		{#if actions.includes('rotate')}
			<button
				type="button"
				class="btn-primary"
				disabled={working}
				bind:this={rotateButton}
				onclick={() => (confirming = 'rotate')}
				data-testid="scim-rotate"
			>
				{m('org.scim.rotate')}
			</button>
		{/if}
		{#if actions.includes('revoke')}
			<button
				type="button"
				class="btn-danger"
				disabled={working}
				onclick={() => (confirming = 'revoke')}
				data-testid="scim-revoke"
			>
				{m('org.scim.revoke')}
			</button>
		{/if}
	{/snippet}
</OrgSettingPanel>

<Modal
	open={confirming !== null}
	ariaLabel={confirming === 'revoke'
		? m('org.scim.confirm.revokeAria')
		: m('org.scim.confirm.rotateAria')}
	width="sm"
	onclose={() => (confirming = null)}
>
	<h2>
		{confirming === 'revoke'
			? m('org.scim.confirm.revokeHeading')
			: m('org.scim.confirm.rotateHeading')}
	</h2>
	<p class="modal-hint" data-testid="scim-confirm-body">
		{confirming === 'revoke' ? m('org.scim.confirm.revokeBody') : m('org.scim.confirm.rotateBody')}
	</p>
	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={() => (confirming = null)}>
			{m('common.cancel')}
		</button>
		{#if confirming === 'revoke'}
			<button type="button" class="btn-danger" onclick={revoke} data-testid="scim-confirm">
				{m('org.scim.revoke')}
			</button>
		{:else}
			<button type="button" class="btn-primary" onclick={() => generate()} data-testid="scim-confirm">
				{m('org.scim.rotate')}
			</button>
		{/if}
	</div>
</Modal>

<StepUpPrompt
	open={stepUpOpen}
	operation="scim_token_mint"
	hint={m('org.scim.stepUpHint')}
	busy={working}
	error={stepUpError}
	onproof={(proof) => generate(proof)}
	oncancel={() => {
		stepUpOpen = false;
		stepUpError = null;
	}}
/>

<SecretReveal
	open={minted !== null}
	ariaLabel={m('org.scim.reveal.aria')}
	heading={m('org.scim.reveal.heading')}
	warningStrong={m('org.scim.reveal.warningStrong')}
	warning={m('org.scim.reveal.warning')}
	secret={minted?.token ?? ''}
	testId="scim-token-value"
	copyLabel={m('org.scim.reveal.copy')}
	copiedLabel={m('org.scim.reveal.copied')}
	copiedToast={m('org.scim.reveal.copiedToast')}
	copyFailedToast={m('org.scim.reveal.copyFailed')}
	doneLabel={m('org.scim.reveal.done')}
	meta={minted
		? [
				{ label: m('org.scim.endpointLabel'), value: endpoint, mono: true },
				{ label: m('org.scim.reveal.prefix'), value: minted.bearer_hash_prefix, mono: true }
			]
		: []}
	onclose={dismissMinted}
/>

<style>
	.status {
		font-size: 0.9rem;
		color: var(--text);
		margin: 0 0 14px;
	}

	.endpoint label {
		display: block;
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 4px;
	}

	.endpoint input {
		width: 100%;
		max-width: 480px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
	}

	.field-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 4px 0 0;
	}

	.no-factor {
		font-size: 0.85rem;
		color: var(--danger);
		margin: 0 0 14px;
	}

	.help-link {
		font-size: 0.85rem;
		margin: 14px 0 0;
	}
</style>
