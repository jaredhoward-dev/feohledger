<script lang="ts">
	/**
	 * Organization → PO match rules (decisions §512).
	 *
	 * `settings.matching`: the amount tolerance and the inspection requirement
	 * the PO match applies, for the whole organization and per vendor / per
	 * header GL account (the "commodity"). Its one writer is the audited
	 * `/api/organization/match-rules` router; the generic organization save
	 * refuses to change it. Precedence is per field — vendor, then GL account,
	 * then organization, then built-in (`services/matching_rules`).
	 *
	 * Validation is the server's: a refused value comes back as a coded
	 * refusal that `api.ts` already states in the reader's language, and this
	 * panel shows it as is — it never restates the range.
	 *
	 * A stored tolerance outside 0–100 (an old unaudited write the PO match
	 * still applies) comes back flagged `tolerance_out_of_range`; the panel
	 * marks it and asks for a fix, and never rewrites it on its own.
	 *
	 * Form conventions (`frontend/docs/ui-patterns.md`): a missing vendor / GL
	 * account is a field error on that picker (`aria-invalid`, focus moved
	 * there); a failed GL-account list is an error with Retry, never an empty
	 * select; the armed Remove disarms on a click anywhere else; opening the
	 * rule form focuses its first field and closing it returns focus to the
	 * Add / Edit control that opened it. Tolerances are pre-filled and hinted
	 * in the reader's decimal notation (`toleranceInput` / `toleranceExample`).
	 *
	 * Admin-only end to end: `readOnly` renders the notice and makes no read.
	 */
	import { tick } from 'svelte';
	import OrgSettingPanel from '#lib/components/organization/OrgSettingPanel.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import VendorPicker from '#lib/components/ui/VendorPicker.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import {
		deleteCommodityMatchRule,
		deleteVendorMatchRule,
		getMatchRules,
		saveCommodityMatchRule,
		saveOrganizationMatchRule,
		saveVendorMatchRule
	} from '#lib/api/matchRules.ts';
	import { listGlAccounts } from '#lib/api/glAccounts.ts';
	import type { GlAccount } from '#lib/types/glAccount.ts';
	import {
		inspectionChoice,
		inspectionFromChoice,
		inspectionLabelKey,
		isLooserThanOrganization,
		organizationTolerance,
		toleranceBody,
		toleranceExample,
		toleranceInput,
		type InspectionChoice,
		type MatchRuleFields,
		type MatchRules
	} from '#lib/types/matchRules.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatPercent } from '#lib/utils/percent.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';

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
	const INSPECTION_CHOICES: InspectionChoice[] = ['inherit', 'required', 'not_required'];
	const PCT = { maximumFractionDigits: 2 };

	let rules = $state<MatchRules | null>(null);
	let loading = $state(false);
	let loadFailed = $state(false);
	let requested = false;

	// Organization layer form.
	let orgTolerance = $state('');
	let orgInspection = $state<InspectionChoice>('inherit');
	let orgSaving = $state(false);

	// Rule form: `editing` names the rule being edited; null = adding.
	type Scope = 'vendor' | 'commodity';
	let formOpen = $state(false);
	let editing = $state<{ scope: Scope; key: string; label: string } | null>(null);
	let scope = $state<Scope>('vendor');
	let vendorId = $state('');
	let glCode = $state('');
	let ruleTolerance = $state('');
	let ruleInspection = $state<InspectionChoice>('inherit');
	let ruleSaving = $state(false);
	let removing = $state<string | null>(null);
	/** Removing loosens the match for that vendor / GL account, so it takes a
	 *  second click: the first arms the button, the second removes. */
	let armed = $state<string | null>(null);
	let glAccounts = $state<GlAccount[]>([]);
	let glLoaded = false;
	let glLoading = $state(false);
	let glLoadFailed = $state(false);
	/** The vendor / GL account was not chosen on submit (a field error). */
	let targetError = $state<string | null>(null);
	let formEl = $state<HTMLFormElement | null>(null);
	let addButton = $state<HTMLButtonElement | null>(null);
	/** The Add / Edit control that opened the form — where focus goes back. */
	let formTrigger: HTMLElement | null = null;

	const glCodes = $derived(
		[...new Map(glAccounts.map((a) => [a.code, a])).values()].sort((a, b) =>
			a.code.localeCompare(b.code)
		)
	);
	const ruleCount = $derived(
		rules ? rules.vendor_rules.length + rules.commodity_rules.length : 0
	);
	const anyOutOfRange = $derived(
		!!rules &&
			[rules.organization, ...rules.vendor_rules, ...rules.commodity_rules].some(
				(r) => r.tolerance_out_of_range
			)
	);

	function apply(next: MatchRules) {
		rules = next;
		orgTolerance = toleranceInput(next.organization.tolerance_pct);
		orgInspection = inspectionChoice(next.organization.require_inspection);
	}

	async function load() {
		loading = true;
		loadFailed = false;
		try {
			apply(await getMatchRules());
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

	/** Load the chart once. A failure keeps the LoadError (and the Retry the
	 *  user pressed) mounted through the retry; when a retry succeeds, focus
	 *  moves to the select that replaces it rather than falling to `<body>`. */
	async function ensureGlAccounts() {
		if (glLoaded || glLoading) return;
		const retrying = glLoadFailed;
		glLoading = true;
		try {
			glAccounts = await listGlAccounts({ active_only: true });
			glLoaded = true;
			glLoadFailed = false;
		} catch {
			glLoadFailed = true;
		} finally {
			glLoading = false;
		}
		if (retrying && glLoaded) {
			await tick();
			formEl?.querySelector<HTMLElement>('[data-testid="match-rule-gl"]')?.focus();
		}
	}

	function tolerancePhrase(value: string | null): string {
		return value === null ? m('org.matchRules.inherits') : formatPercent(value, PCT);
	}

	function inspectionPhrase(value: boolean | null): string {
		return m(inspectionLabelKey(inspectionChoice(value)));
	}

	async function saveOrganization() {
		orgSaving = true;
		try {
			apply(
				await saveOrganizationMatchRule({
					tolerance_pct: toleranceBody(orgTolerance),
					require_inspection: inspectionFromChoice(orgInspection)
				})
			);
			toast(m('org.matchRules.saved'), 'success');
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			orgSaving = false;
		}
	}

	/** Open the form and move focus to its first field (WCAG 2.4.3). */
	async function showForm() {
		formTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		formOpen = true;
		await tick();
		formEl?.querySelector<HTMLElement>('input:not([type="hidden"]), select')?.focus();
	}

	function openAdd() {
		editing = null;
		scope = 'vendor';
		vendorId = '';
		glCode = '';
		ruleTolerance = '';
		ruleInspection = 'inherit';
		targetError = null;
		void ensureGlAccounts();
		void showForm();
	}

	function openEdit(kind: Scope, key: string, label: string, rule: MatchRuleFields) {
		editing = { scope: kind, key, label };
		scope = kind;
		ruleTolerance = toleranceInput(rule.tolerance_pct);
		ruleInspection = inspectionChoice(rule.require_inspection);
		targetError = null;
		void showForm();
	}

	/** Close the form and return focus to the control that opened it — the
	 *  row's Edit, or (once the form is gone and it is back) Add rule. */
	async function closeForm() {
		formOpen = false;
		editing = null;
		targetError = null;
		const back = formTrigger;
		formTrigger = null;
		await tick();
		if (back?.isConnected && !(back as HTMLButtonElement).disabled) back.focus();
		else addButton?.focus();
	}

	function clearTargetError() {
		targetError = null;
	}

	async function saveRule(event: SubmitEvent) {
		event.preventDefault();
		const key = editing?.key ?? (scope === 'vendor' ? vendorId : glCode);
		if (!key) {
			targetError = m(scope === 'vendor' ? 'org.matchRules.pickVendor' : 'org.matchRules.pickGl');
			await tick();
			formEl
				?.querySelector<HTMLElement>(
					scope === 'vendor' ? '[data-testid="match-rule-vendor"]' : '[data-testid="match-rule-gl"]'
				)
				?.focus();
			return;
		}
		const body = {
			tolerance_pct: toleranceBody(ruleTolerance),
			require_inspection: inspectionFromChoice(ruleInspection)
		};
		ruleSaving = true;
		try {
			apply(
				scope === 'vendor'
					? await saveVendorMatchRule(key, body)
					: await saveCommodityMatchRule(key, body)
			);
			toast(m('org.matchRules.ruleSaved'), 'success');
			void closeForm();
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			ruleSaving = false;
		}
	}

	async function removeRule(kind: Scope, key: string) {
		const id = `${kind}:${key}`;
		if (armed !== id) {
			armed = id;
			return;
		}
		armed = null;
		removing = id;
		try {
			apply(
				kind === 'vendor' ? await deleteVendorMatchRule(key) : await deleteCommodityMatchRule(key)
			);
			toast(m('org.matchRules.ruleRemoved'), 'success');
			if (editing?.key === key) void closeForm();
		} catch (err) {
			toast(errorMessage(err, m('org.toast.saveFailed')), 'error');
		} finally {
			removing = null;
		}
	}
	/** The armed Remove disarms on a click anywhere outside a row action
	 *  (`docs/ui-patterns.md` § armed-confirm; `routes/gl-accounts`). */
	function disarmOnOutsideClick(e: MouseEvent) {
		if (armed && !(e.target as HTMLElement | null)?.closest('.row-action')) armed = null;
	}
</script>

<svelte:window onclick={disarmOnOutsideClick} />

{#snippet ruleRow(kind: Scope, key: string, label: string, rule: MatchRuleFields)}
	<tr data-testid="match-rule-row">
		<td>
			<span class="scope">{m(kind === 'vendor' ? 'org.matchRules.scope.vendor' : 'org.matchRules.scope.commodity')}</span>
			<span class="target">{label}</span>
		</td>
		<td>
			{tolerancePhrase(rule.tolerance_pct)}
			{#if rule.tolerance_out_of_range}
				<Badge tone="danger">{m('org.matchRules.outOfRange')}</Badge>
			{/if}
			{#if rules && isLooserThanOrganization(rule, rules)}
				<Badge tone="warning">{m('org.matchRules.looser')}</Badge>
			{/if}
		</td>
		<td>{inspectionPhrase(rule.require_inspection)}</td>
		<td class="actions">
			<RowAction
				ariaLabel={m('org.matchRules.editFor', { target: label })}
				onclick={() => openEdit(kind, key, label, rule)}
			>
				{m('org.matchRules.edit')}
			</RowAction>
			<RowAction
				variant="danger"
				armed={armed === `${kind}:${key}`}
				busy={removing === `${kind}:${key}`}
				ariaLabel={armed === `${kind}:${key}`
					? m('org.matchRules.confirmRemoveFor', { target: label })
					: m('org.matchRules.removeFor', { target: label })}
				onclick={() => removeRule(kind, key)}
			>
				{armed === `${kind}:${key}` ? m('org.matchRules.confirmRemove') : m('org.matchRules.remove')}
			</RowAction>
		</td>
	</tr>
{/snippet}

<OrgSettingPanel
	title={m('org.section.matchRules')}
	intro={m('org.matchRules.intro')}
	testId="match-rules"
	{readOnly}
	loading={loading || (rules === null && !loadFailed)}
	{loadFailed}
	loadFailedMessage={m('org.matchRules.loadFailed')}
	onretry={load}
>
	{#if rules}
		<p class="precedence" id="{uid}-precedence">{m('org.matchRules.precedence')}</p>
		{#if anyOutOfRange}
			<p class="out-of-range-notice" data-testid="match-rules-out-of-range">
				{m('org.matchRules.outOfRangeNotice')}
			</p>
		{/if}

		<fieldset class="group" data-testid="match-rules-organization">
			<legend>{m('org.matchRules.orgHeading')}</legend>
			<p class="field-hint">
				{m('org.matchRules.orgInForce', {
					tolerance: formatPercent(organizationTolerance(rules), PCT),
					builtIn: formatPercent(rules.built_in.tolerance_pct, PCT)
				})}
			</p>
			<div class="row">
				<label for="{uid}-org-tol">
					<span>{m('org.matchRules.toleranceLabel')}</span>
					<input
						id="{uid}-org-tol"
						type="text"
						inputmode="decimal"
						autocomplete="off"
						bind:value={orgTolerance}
						placeholder={formatPercent(rules.built_in.tolerance_pct, PCT)}
						aria-invalid={rules.organization.tolerance_out_of_range ? 'true' : undefined}
						aria-describedby={rules.organization.tolerance_out_of_range
							? `${uid}-org-tol-range ${uid}-tol-hint`
							: `${uid}-tol-hint`}
						disabled={orgSaving}
						data-testid="match-rules-org-tolerance"
					/>
				</label>
				<label for="{uid}-org-insp">
					<span>{m('org.matchRules.inspectionLabel')}</span>
					<select
						id="{uid}-org-insp"
						bind:value={orgInspection}
						disabled={orgSaving}
					>
						{#each INSPECTION_CHOICES as choice (choice)}
							<option value={choice}>
								{choice === 'inherit'
									? m('org.matchRules.inspection.builtIn')
									: m(inspectionLabelKey(choice))}
							</option>
						{/each}
					</select>
				</label>
			</div>
			{#if rules.organization.tolerance_out_of_range}
				<p id="{uid}-org-tol-range" class="field-error" data-testid="match-rules-org-out-of-range">
					{m('org.matchRules.orgOutOfRange')}
				</p>
			{/if}
			<p id="{uid}-tol-hint" class="field-hint">
				{m('org.matchRules.toleranceHint', { example: toleranceExample() })}
			</p>
			<button
				type="button"
				class="btn-secondary"
				disabled={orgSaving}
				onclick={saveOrganization}
				data-testid="match-rules-org-save"
			>
				{orgSaving ? m('org.common.saving') : m('org.matchRules.saveOrg')}
			</button>
		</fieldset>

		<h3 class="sub">{m('org.matchRules.rulesHeading')}</h3>
		{#if ruleCount === 0}
			<p class="empty" data-testid="match-rules-empty">{m('org.matchRules.empty')}</p>
		{:else}
			<div class="table-wrap">
				<table data-testid="match-rules-table">
					<caption class="visually-hidden">{m('org.matchRules.rulesHeading')}</caption>
					<thead>
						<tr>
							<th scope="col">{m('org.matchRules.col.appliesTo')}</th>
							<th scope="col">{m('org.matchRules.col.tolerance')}</th>
							<th scope="col">{m('org.matchRules.col.inspection')}</th>
							<th scope="col"><span class="visually-hidden">{m('org.matchRules.col.actions')}</span></th>
						</tr>
					</thead>
					<tbody>
						{#each rules.vendor_rules as rule (rule.vendor_id)}
							{@render ruleRow(
								'vendor',
								rule.vendor_id,
								rule.vendor_name ?? m('org.matchRules.deletedVendor'),
								rule
							)}
						{/each}
						{#each rules.commodity_rules as rule (rule.gl_account)}
							{@render ruleRow(
								'commodity',
								rule.gl_account,
								rule.gl_account_name ? `${rule.gl_account} — ${rule.gl_account_name}` : rule.gl_account,
								rule
							)}
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if formOpen}
			<form
				class="group"
				onsubmit={saveRule}
				novalidate
				bind:this={formEl}
				data-testid="match-rule-form"
			>
				<fieldset>
					<legend>
						{editing
							? m('org.matchRules.editHeading', { target: editing.label })
							: m('org.matchRules.addHeading')}
					</legend>
					{#if !editing}
						<div class="scope-choice" role="radiogroup" aria-label={m('org.matchRules.scopeLabel')}>
							<label>
								<input
									type="radio"
									name="{uid}-scope"
									value="vendor"
									bind:group={scope}
									onchange={clearTargetError}
								/>
								{m('org.matchRules.scope.vendor')}
							</label>
							<label>
								<input
									type="radio"
									name="{uid}-scope"
									value="commodity"
									bind:group={scope}
									onchange={clearTargetError}
								/>
								{m('org.matchRules.scope.commodity')}
							</label>
						</div>
						{#if scope === 'vendor'}
							<VendorPicker
								bind:value={vendorId}
								label={m('org.matchRules.vendorLabel')}
								placeholder={m('org.matchRules.vendorPlaceholder')}
								testid="match-rule-vendor"
								invalid={!!targetError}
								errorId={targetError ? `${uid}-target-error` : undefined}
								onselect={clearTargetError}
							/>
						{:else if glLoadFailed}
							<LoadError
								message={m('org.matchRules.glLoadFailed')}
								onretry={ensureGlAccounts}
								retrying={glLoading}
								testId="match-rule-gl-load-error"
							/>
						{:else}
							<label for="{uid}-gl">
								<span>{m('org.matchRules.glLabel')}</span>
								<select
									id="{uid}-gl"
									bind:value={glCode}
									onchange={clearTargetError}
									disabled={glLoading}
									aria-busy={glLoading ? 'true' : undefined}
									aria-invalid={targetError ? 'true' : undefined}
									aria-describedby={targetError ? `${uid}-target-error` : undefined}
									data-testid="match-rule-gl"
								>
									<option value="">{m('org.matchRules.glPlaceholder')}</option>
									{#each glCodes as account (account.code)}
										<option value={account.code}>{account.code} — {account.name}</option>
									{/each}
								</select>
							</label>
						{/if}
						{#if targetError}
							<p class="field-error" id="{uid}-target-error" data-testid="match-rule-target-error">
								{targetError}
							</p>
						{/if}
					{/if}
					<div class="row">
						<label for="{uid}-rule-tol">
							<span>{m('org.matchRules.toleranceLabel')}</span>
							<input
								id="{uid}-rule-tol"
								type="text"
								inputmode="decimal"
								autocomplete="off"
								bind:value={ruleTolerance}
								placeholder={m('org.matchRules.inherits')}
								aria-describedby="{uid}-rule-tol-hint"
								data-testid="match-rule-tolerance"
							/>
						</label>
						<label for="{uid}-rule-insp">
							<span>{m('org.matchRules.inspectionLabel')}</span>
							<select
								id="{uid}-rule-insp"
								bind:value={ruleInspection}
								data-testid="match-rule-inspection"
							>
								{#each INSPECTION_CHOICES as choice (choice)}
									<option value={choice}>{m(inspectionLabelKey(choice))}</option>
								{/each}
							</select>
						</label>
					</div>
					<p id="{uid}-rule-tol-hint" class="field-hint">
						{m('org.matchRules.ruleToleranceHint', { example: toleranceExample() })}
					</p>
				</fieldset>
				<div class="form-actions">
					<button type="submit" class="btn-primary" disabled={ruleSaving} data-testid="match-rule-save">
						{ruleSaving ? m('org.common.saving') : m('org.matchRules.saveRule')}
					</button>
					<button
						type="button"
						class="btn-secondary"
						disabled={ruleSaving}
						onclick={() => void closeForm()}
						data-testid="match-rule-cancel"
					>
						{m('org.matchRules.cancel')}
					</button>
				</div>
			</form>
		{/if}
	{/if}

	{#snippet footer()}
		{#if rules && !formOpen}
			<button
				type="button"
				class="btn-primary"
				bind:this={addButton}
				onclick={openAdd}
				data-testid="match-rule-add"
			>
				{m('org.matchRules.add')}
			</button>
		{/if}
	{/snippet}
</OrgSettingPanel>

<style>
	.out-of-range-notice {
		margin: 0 0 16px;
		padding: 8px 12px;
		border-radius: 6px;
		background: var(--danger-tint);
		color: var(--danger-on-tint);
		font-size: 0.85rem;
	}

	.field-error {
		font-size: 0.78rem;
		color: var(--danger);
		margin: 4px 0 8px;
	}

	.precedence {
		margin: 0 0 16px;
		font-size: 0.85rem;
		color: var(--text);
	}

	.group {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 12px 14px;
		margin: 0 0 16px;
	}

	.group fieldset {
		border: 0;
		padding: 0;
		margin: 0;
	}

	legend {
		font-weight: 600;
		font-size: 0.9rem;
		padding: 0 4px;
	}

	.row {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		margin: 8px 0;
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.85rem;
		color: var(--text);
	}

	input[type='text'],
	select {
		min-height: 32px;
		min-width: 12rem;
		padding: 4px 8px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--text);
		font: inherit;
	}

	.scope-choice {
		display: flex;
		gap: 16px;
		margin: 8px 0;
	}

	.scope-choice label {
		flex-direction: row;
		align-items: center;
		gap: 6px;
		min-height: 24px;
	}

	.field-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 4px 0 8px;
	}

	.sub {
		font-size: 0.95rem;
		margin: 8px 0;
	}

	.empty {
		font-size: 0.85rem;
		color: var(--text-muted);
	}

	.table-wrap {
		overflow-x: auto;
		margin-bottom: 16px;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}

	th,
	td {
		text-align: start;
		padding: 8px;
		border-bottom: 1px solid var(--border);
		vertical-align: middle;
	}

	th {
		font-weight: 600;
		color: var(--text-muted);
	}

	.scope {
		display: block;
		font-size: 0.75rem;
		color: var(--text-muted);
	}

	.actions {
		white-space: nowrap;
		text-align: end;
	}

	.actions :global(.row-action + .row-action) {
		margin-inline-start: 6px;
	}

	.form-actions {
		display: flex;
		gap: 8px;
		margin-top: 8px;
	}
</style>
