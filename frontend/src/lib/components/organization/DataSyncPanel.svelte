<script lang="ts">
	/**
	 * Organization → Data sync: a signpost to the three pages that each carry
	 * their own Sync-from-ERP action (decisions §161). No state, no writes.
	 */
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
</script>

<!-- Data Sync is a SIGNPOST, not a second set of sync buttons.
     Each of the three ERP-synced data sets now has its own page
     carrying its own Sync-from-ERP action gated on `auth.isManager`
     — `/gl-accounts` (new), `/purchase-orders`, `/vendors` — which
     matches every one of those endpoints' real gate
     (admin | ap_manager). The two buttons that used to live here
     were admin-only by virtue of this route's nav gate, said nothing
     about which entity's chart they would write into, and reported
     into a bare <span> with no list to refresh; the vendors row had
     already been a link for exactly that reason. Three links is one
     vocabulary for one fact (decisions §153's objection to a panel
     growing a second shape, and §161 for the call).
     It keeps its place because this is where the ERP connection is
     configured, so "now where do I pull it?" is asked here. -->
<section class="card">
	<div class="help-row">
		<h2>{m('org.section.dataSync')}</h2>
		<HelpTip term="erp-sync" />
	</div>
	<p class="card-hint">{m('org.dataSync.hint')}</p>

	<div class="sync-grid">
		<div class="sync-item">
			<div class="sync-info">
				<span class="sync-name">{m('org.dataSync.coa')}</span>
				<span class="sync-desc">{m('org.dataSync.coaDesc')}</span>
			</div>
			<a href="/gl-accounts" class="btn-outline">{m('org.dataSync.manageCoa')}</a>
		</div>

		<div class="sync-item">
			<div class="sync-info">
				<span class="sync-name">{m('org.dataSync.pos')}</span>
				<span class="sync-desc">{m('org.dataSync.posDesc')}</span>
			</div>
			<a href="/purchase-orders" class="btn-outline">{m('org.dataSync.managePos')}</a>
		</div>

		<div class="sync-item">
			<div class="sync-info">
				<span class="sync-name">{m('org.dataSync.vendors')}</span>
				<span class="sync-desc">{m('org.dataSync.vendorsDesc')}</span>
			</div>
			<a href="/vendors" class="btn-outline">{m('org.dataSync.manageVendors')}</a>
		</div>
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

	.sync-grid {
		display: flex;
		flex-direction: column;
		gap: 12px;
		margin-top: 14px;
	}

	.sync-item {
		display: flex;
		align-items: center;
		/* The description and its action cannot share a line at 320px, so the
		   action drops below the description instead of pushing the document
		   sideways (WCAG 1.4.10). */
		flex-wrap: wrap;
		gap: 12px;
		padding: 10px 12px;
		background: var(--bg);
		border-radius: 6px;
	}

	.sync-info {
		flex: 1;
		/* `flex: 1` leaves `min-width: auto`, so this column refuses to shrink
		   below its longest word plus the action beside it. Zero lets it give
		   way and the text wrap. */
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.sync-name {
		font-size: 0.88rem;
		font-weight: 500;
		color: var(--text);
	}

	.sync-desc {
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.btn-outline {
		padding: 8px 18px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		/* Deliberately NOT `white-space: nowrap` (unlike `.btn-test`, whose label
		   is two short words). These labels name their destination — "Manage on
		   Chart of Accounts page" is 265px — so a nowrap label is wider than a
		   320px viewport's whole content column and no amount of wrapping the
		   ROW can rescue it: the item would still overflow on its own line.
		   Letting the label wrap is what makes the page reflow (WCAG 1.4.10).
		   At every width where the label fits, shrink-to-fit keeps it on one
		   line, so nothing changes above ~460px. */
		text-decoration: none;
		display: inline-block;
		text-align: center;
	}

	.btn-outline:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
</style>
