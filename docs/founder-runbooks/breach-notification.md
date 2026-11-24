# Personal-data breach notification — the 72-hour runbook

**Why this matters**: GDPR Art. 33 gives you **72 hours** from *becoming aware*
of a personal-data breach to notify the lead supervisory authority — and the
clock does not stop for weekends, incomplete information, or "we're still
investigating". Miss it and the fine is administrative (up to €10M / 2% of
global turnover) on top of whatever the breach itself costs. This runbook is the
one-page procedure to run the moment you suspect a breach, so you're not
improvising the process while the clock runs.

**The 72 hours is not the shortest clock.** Three of the platforms FeohLedger
connects to — Xero, Intuit (QuickBooks Online) and Shopify — require a report
**within 24 hours** of discovering an incident, and Intuit's trigger is wider
than "personal data". § 3a lists them; work it in parallel with the
assessment, never after it.

This is the personal-data-breach companion to the operational incident process
in [`support-and-status.md`](./support-and-status.md) § "Incident communication
runbook". An availability outage (site down) is *not* automatically a
personal-data breach; a personal-data breach may or may not also be an outage.
Run both runbooks when both apply.

> **Not legal advice.** This is an engineering/operational runbook. For any
> non-obvious call — especially "is this notifiable?" and "which authority?" —
> get your privacy counsel or DPO on the line. Ask counsel if unsure.

---

## 0. Roles + contacts (fill this in NOW, before an incident)

A breach is the wrong time to discover you don't know who decides. Pre-assign:

| Role | Who | Responsibility |
|------|-----|----------------|
| **Incident Lead** | _(founder / on-call — see [`support-and-status.md`](./support-and-status.md) § On-call)_ | Owns the timeline, makes the go/no-go on notification |
| **Privacy / DPO** | _(counsel or named DPO)_ | Notifiability call, authority liaison, drafts the Art. 33 notice |
| **Technical Lead** | _(engineer on-call)_ | Containment, forensics, scope determination |
| **Comms** | _(founder)_ | Customer + data-subject messaging, status page |

External contacts to have on file:
- **Lead supervisory authority** — your one-stop-shop DPA (the authority of your
  EU main establishment, or — if you have no EU establishment — the authority of
  each member state where affected individuals are). Record the exact authority,
  its online breach-report form URL, and account login.
- **Privacy counsel** — phone + email.
- **Cyber-insurance breach hotline** — see [`insurance.md`](./insurance.md);
  many policies *require* you to call them before notifying anyone, and they
  provide breach-counsel + forensics.
- **Connected-platform security channels** — the Xero, Intuit, Sage,
  Shopify, Microsoft, Merge.dev and Lightspeed reporting addresses in § 3a,
  and **which email address each of our developer accounts is registered
  under** (Intuit and Xero send their replies there). Check the table's "re-verify by" dates while you are here.
- **Affected sub-processor(s)** contact — from
  [`../sub-processors.md`](../sub-processors.md) (a sub-processor breach is
  *your* breach to the controller; they owe you notice "without undue delay" per
  Art. 28).

---

## 1. Detection + triage (hour 0)

A breach can surface from: an alert, a customer report, a sub-processor notice,
a security researcher, or your own audit-log review.

The moment a credible signal arrives:

1. **Start the clock.** Record the UTC timestamp of *awareness* — this is the
   start of the 72 hours. "Awareness" means a reasonable degree of certainty
   that a security incident compromising personal data has occurred — not
   certainty about full scope. Don't wait for a complete picture to start the
   clock or you'll blow the deadline. The same timestamp starts the
   **24-hour connected-platform clocks** in § 3a, which do not wait for the
   § 2 assessment.
2. **Open an incident record.** One canonical doc (timeline, decisions,
   evidence links). Every entry timestamped UTC.
3. **Assign the Incident Lead + Technical Lead.** Page per
   [`support-and-status.md`](./support-and-status.md) § On-call.
4. **Preserve evidence before you change anything** — see § 5. Pull the
   relevant audit trail *first*; it's WORM and immutable (see § 5), so it's your
   ground truth for who-touched-what.

## 2. Is it a personal-data breach? (severity assessment)

GDPR Art. 4(12): a breach of security leading to the accidental or unlawful
**destruction, loss, alteration, unauthorized disclosure of, or access to**
personal data. Three flavors — a breach can be more than one:

- **Confidentiality** — unauthorized disclosure/access (the classic "data leak").
- **Integrity** — unauthorized alteration.
- **Availability** — loss/destruction of access (ransomware, deleted data, a
  prolonged outage that loses data).

Decision gate:

| Question | If NO | If YES |
|----------|-------|--------|
| Did the incident involve **personal data**? (invoice party names, vendor contacts, user accounts, tax IDs, bank details, supplier-chat content — see [`../sub-processors.md`](../sub-processors.md) data-category legend) | Not a GDPR personal-data breach. Still log it; it may be a security incident or a confidentiality issue for business data. | Continue. |
| Is there a **risk to the rights and freedoms** of individuals? | Breach is recorded internally (Art. 33(5) record-keeping) but **may not be notifiable** — document the reasoning. | Notify the authority (§ 4). |
| Is the risk **HIGH** (likely significant harm — identity theft, fraud, financial loss, exposure of tax IDs / bank details)? | Authority notification only. | **Also notify data subjects** (§ 6). |

Factors that push risk **up**: special-category or financial data (this app
holds bank details + tax IDs), large volume, vulnerable individuals, data not
encrypted/pseudonymized, easy re-identification, malicious actor.

Factors that pull risk **down**: data was strongly encrypted and the key was not
compromised; the breach is fully contained with no realistic access; data is
already public.

> **Our mitigants** (use to argue risk down — only where true for *this*
> incident): bank/PAN/raw-TIN values are minimized to last-4 in the DB (full
> values live only in encrypted payment files); secrets are KMS-encrypted;
> tenant DBs are isolated; audit log is immutable. See root `CLAUDE.md` §
> "Project invariants". Don't overclaim — verify each applies to the actual
> data exposed.

## 3. Containment (in parallel with assessment — don't serialize)

Stop the bleeding without destroying evidence:

- **Cut the access path**: rotate the compromised credential/key (SOPS+KMS —
  see [`../secrets-rotation.md`](../secrets-rotation.md)), revoke sessions (JWT
  blocklist), disable the affected user/SSO, kill the leaking integration
  (disable the adapter / pull the credential so it fails closed to `mock`).
- **Isolate** the affected tenant/component if isolation limits spread (the
  database-per-tenant design means one tenant's breach usually doesn't widen to
  others — confirm, then state it).
- **Patch** the root cause. Do **not** "code around" it (root `CLAUDE.md`
  rail 4) — fix the actual vulnerability.
- Snapshot before destructive remediation so forensics survive.

## 3a. Connected-platform notices — the 24-hour leg

When FeohLedger is connected to an accounting or commerce platform through an
app **registered under our own developer account**, that platform's developer
terms bind us directly, and several of them set a reporting clock far shorter
than GDPR's. These are contract duties to the platform, separate from the
regulator (§ 4), the customer (§ 6) and data subjects (§ 6). Run this step
from T+0, in parallel with § 2 and § 3.

**Does a row apply?** Ask two questions for each platform below:

1. **Could the incident touch that platform's data or our connection to it?**
   Its OAuth tokens or client secret (sealed in the tenant's credential store,
   `backend/docs/erp-integration.md`), bills, vendors or chart of accounts we
   read from it, or the systems that hold them. When unsure, treat it as yes:
   a report that turns out unnecessary costs an email; a missed one breaches
   the terms and can get the app suspended for every tenant.
2. **Whose app made the connection?** QuickBooks Online, Xero, Sage Business
   Cloud Accounting and Blackbaud FE NXT connect through the **platform app**
   (ours) unless a tenant saved **its own app's** client ID and secret on the
   ERP Integration page. For a tenant's own app, and for Shopify (the tenant
   creates the custom app in its own store admin and pastes us its token —
   `backend/docs/commerce-integration.md`), the duty is the **tenant's**, not
   ours. Tell that tenant in the § 6 controller notice, straight away, that
   the platform's own clock is running for them. Business Central, NetSuite,
   SYSPRO, Square, Lightspeed, Cin7 and WooCommerce also run on credentials
   the tenant creates in its own account; each row says whether the
   platform's terms still reach us (Microsoft's and Lightspeed's do).

**Trigger differences that matter.** Intuit's "Security Incident" covers any
event that compromises, or is reasonably likely to compromise, the
confidentiality, integrity or availability of *systems used with the app* —
so it can fire even when § 2 concludes there was no personal-data breach.
Shopify counts *suspected* breaches of merchant data. Xero's trigger is a
security issue affecting the app, unauthorised access to user data being the
example it gives.

| Platform | Notice duty | Clock | Channel | Notice must include | Source (checked) |
|---|---|---|---|---|---|
| **Xero** | Yes, to Xero | Immediately; **within 24 h of discovery** | `api@support.xero.com` (not `legalnotices@xero.com`, which is the general legal-notice address) | Description of the incident and your action plan to contain and mitigate it. You must also notify affected users and any regulators yourself, and may not name Xero in those notices without Xero's written OK | [Xero Developer Platform Terms](https://developer.xero.com/xero-developer-platform-terms-conditions), clause 15 (version of 4 Dec 2025; binds earlier developers from 2 Mar 2026). Checked 2026-11-22 |
| **Intuit** (QuickBooks Online) | Yes, to Intuit | Without undue delay; **no later than 24 h after discovery** | Not named in § 9.3. Formal notices (§ 16.7) go by courier or registered mail to the Intuit contracting party, too slow for 24 h: before go-live, ask Intuit developer support which channel it accepts for § 9.3 reports and record it here | Detailed description of the incident and the data affected, and anything else Intuit reasonably asks. Intuit then sets a remediation deadline (Critical 7 days, High 30, Medium 90, from discovery), may require an approved third-party audit (§ 9.5), and directs any notice to users or regulators at our cost | [Intuit Developer Terms of Service](https://developer.intuit.com/app/developer/qbo/docs/legal-agreements/intuit-terms-of-service-for-intuit-developer-services), §§ 9.3–9.5, 16.7, 17 (version of 12 Nov 2026; binds existing developers from 12 Dec 2026). Checked 2026-11-22 |
| **Shopify** | Yes, to Shopify, from whoever holds the app (today the merchant; see above) | Immediately; **within 24 h of becoming aware**; actual *or suspected* | Shopify Support via `https://help.shopify.com/questions/partners` | Then remedy, investigate, mitigate harm to Shopify and merchants, answer Shopify's questions, and report progress regularly | [Shopify API License and Terms of Use](https://www.shopify.com/legal/api-terms), § 6.2.10 (version of 7 Oct 2026). Checked 2026-11-22 |
| **Sage** (Business Cloud Accounting, via Sage Developer Services) | Yes, to Sage | **Promptly** (no hour count) | Sage Cyber Defence Operations, `cdo@sage.com` | Details of the data breach (or of a vulnerability found in or reported to us) | [Sage Developer Services Licence Agreement](https://developer.sage.com/files/DeveloperServicesLicenseAgreement.pdf), clause 13.6 (version of June 2026). Checked 2026-11-22 |
| **Blackbaud** (FE NXT, SKY API) | **None found.** The terms make the developer fully responsible for the security of data the app processes, with an indemnity, but set no reporting duty or clock | — | Formal notices: certified mail only | — | [SKY API Terms of Use](https://developer.blackbaud.com/skyapi/terms), "Application Security" and "Electronic Communications; Notices" (version of 18 Mar 2022). Checked 2026-11-22 |
| **Sage Intacct** | **Not determined: terms not publicly reachable.** Our app holds a Web Services sender ID under Sage's developer programme (`backend/docs/erp-integration.md` § Sage Intacct direct adapter), so a Sage developer agreement binds us, but its text was not found. Until it is read, treat this row as the Sage row above (promptly, `cdo@sage.com`) as a precaution | — | — | — | Tried 2026-11-24: developer.intacct.com and developer.sage.com/intacct link no licence; the Web Services developer licence comes through the Sage Intacct partner programme, which publishes none; the [Sage Developer Services Licence Agreement](https://developer.sage.com/files/DeveloperServicesLicenseAgreement.pdf) returned HTTP 403, so whether it covers Intacct is unconfirmed. The customer-facing Sage Intacct terms bind the tenant, not us |
| **Sage Accounting** (South Africa) | **None found.** No security or breach-reporting clause; Sage may reset or revoke a compromised API key (cl. 4.2) | — | — | — | [Sage One Accounting API Terms and Conditions](https://accounting.sageone.co.za/ApiDocumentation/API%20Terms%20and%20Conditions.pdf), cl. 4.2 (version 2.0.2, 9 Jul 2014). Checked 2026-11-24 |
| **Microsoft Dynamics 365 Business Central** | Yes, to Microsoft, for a data breach "by you resulting from any aspect of the Microsoft APIs involving your Application or any data collected through your Application", and for vulnerabilities in the app's connection to the APIs. The Entra app is registered in the tenant's own directory, but the terms bind whoever builds the Application that calls the APIs: treat it as ours, and tell the tenant | **Promptly** (no hour count) | Microsoft Security Response Center, `secure@microsoft.com` | Details of the data breach. No public statement about it as it relates to the Microsoft APIs without Microsoft's prior written permission. § 4 survives five years after termination | [Microsoft APIs Terms of Use](https://learn.microsoft.com/en-us/legal/microsoft-apis/terms-of-use), § 4 "Security" (last updated October 2025), which the [Business Central API reference](https://learn.microsoft.com/dynamics-nav/api-reference/v2.0) tells developers to accept. Checked 2026-11-24 |
| **Oracle NetSuite** | **None found for us.** SuiteTalk use runs under the tenant's own NetSuite subscription: the SuiteCloud Technologies Terms of Use grant the API right to the *Customer* and have no notice clause, and the integration record and its credentials are the tenant's. A SuiteCloud Developer Network (partner) agreement would bind us if we joined; its text is not public | — | — | — | [SuiteCloud Technologies Terms of Use](https://www.oracle.com/a/ocom/docs/corporate/cloud-suitecloud-tos-v071023-us-eng.pdf) (v071023). SDN agreement: not publicly reachable (searched oracle.com; only customer-side terms found). Checked 2026-11-24 |
| **SYSPRO** | **None found for us.** The adapter signs in with the tenant's own operator and company credentials against the tenant's SYSPRO, under no SYSPRO developer agreement. Only if we published on the SYSPRO App Store would its Publisher Agreement apply, and its one notice duty covers a breach of the *Publisher Account* (immediately, via the App Store, cl. 1.10) | — | — | — | [SYSPRO App Store Publisher Agreement](https://appstore.syspro.com/Content/Guides/SYSPRO%20App%20Store%20-%20Publisher%20Agreement.pdf), cl. 1.10 (last published Oct 2017); no public developer or API terms found for e.net / REST. Checked 2026-11-24 |
| **Merge.dev** | Yes, to Merge, as its customer (our own account) | **Promptly** (no hour count); trigger is "unauthorized access to or use of the Service" | Not named in § 3.2. Formal notices go by overnight courier or certified mail; Merge's security page publishes `security@merge.dev` for vulnerability reports. Before go-live, ask Merge which channel it accepts for § 3.2 reports and record it here | — (not specified) | [Merge Master Subscription Agreement](https://www.merge.dev/legal/msa), § 3.2 and the notices clause (last updated 9 Sep 2026; a signed Order Form takes precedence); [merge.dev/security](https://merge.dev/security). Checked 2026-11-24 |
| **Lightspeed** (Retail X-Series, formerly Vend) | Yes, to Lightspeed, from the "Developer" using the API. The adapter calls it with the merchant's personal token, so treat it as ours, and tell the merchant | **Immediately** for an actual *or threatened* breach involving the API or personal data (§ 3.3); **promptly** for any security incident with the app (§ 3.2) | Written notice to Lightspeed Commerce Inc., 700 Rue Saint-Antoine E, Montréal, QC H2Y 1A6, Canada, with mandatory copy to `legal@lightspeedhq.com` (§ 8.1); send the email first | Notice of the breach; then work diligently to remedy it as soon as practicable | [Lightspeed API License Agreement](https://developers.lightspeedhq.com/terms), §§ 3.2, 3.3, 8.1 (effective 20 May 2025). The page names no product line; X-Series coverage rests on `vendhq.com/terms`, the old Vend developer terms, redirecting here. Checked 2026-11-24 |
| **Square** | **None found.** "You are fully responsible for the security of data on your site and processed via Your Services" (§ 1(f)); no reporting duty or clock | — | — | — | [Square Developer Terms of Service](https://squareup.com/us/en/legal/general/developers) (US, last updated 27 Oct 2026), § 1(f). The access token comes from the merchant's own Developer Console, so the merchant also holds Square seller terms of its own (not read here). The UK, IE, CA and AU developer terms are separate agreements, not read. Checked 2026-11-24 |
| **Cin7** (Core) | **None found for an app.** The Core Terms of Use bind the subscriber, who generates the application key we hold: "In the event of an unauthorized use of your password, or any other breach of security, Cin7 Americas is to be notified immediately" (§ 2(b)), and third-party applications sit outside the Services (§ 4(e)). That is the **tenant's** duty: tell them. No public Cin7 developer or partner agreement found | Tenant's: immediately | Tenant's: email (§ 12(h): notices in writing via email; no security address named) | — | [Cin7 Core Terms of Use](https://cin7.com/legal/terms-of-use-core/), §§ 2(b), 4(e), 12(h) (effective 14 Aug 2026). Checked 2026-11-24 |
| **WooCommerce** | **None applies.** The adapter uses a REST key the merchant creates in its own self-hosted WordPress store; no Automattic developer agreement is involved (`backend/docs/commerce-integration.md` § WooCommerce). The WooCommerce Marketplace Vendor Agreement binds only sellers listed on woocommerce.com, which we are not | — | The merchant, in the § 6 notice | — | [Marketplace Vendor Agreement](https://woocommerce.com/woo-vendor-agreement/) (scope only; not read for a notice clause). Checked 2026-11-24 |

**Re-verify by:** each platform reserves the right to change its terms on
notice (Intuit rewrote its terms in November 2026). Re-read every row's
source before the first tenant goes live on that platform, and at least every
six months after; update the version and checked date when you do.

**In the incident:** for each row that applies, send the notice inside its
clock with what you know, mark what is still under investigation, and log
the time sent in the incident record. A partial report on time beats a full
one late, as in § 4.

## 4. The 72-hour notification to the supervisory authority (Art. 33)

**Deadline: 72 hours from awareness (§ 1).** If you can't fully investigate in
time, **notify anyway with what you have** — Art. 33(4) explicitly allows
notifying in phases. A late, complete notice is worse than an on-time partial
one. If you miss 72h, you must still notify and include reasons for the delay.

Notify via the lead authority's online breach-report form. Include, at minimum
(Art. 33(3)):

1. **Nature of the breach** — what happened, categories and approximate number
   of **data subjects** affected, categories and approximate number of
   **records** affected.
2. **DPO / contact point** — name + contact where the authority can get more
   information.
3. **Likely consequences** — the probable effect on individuals.
4. **Measures taken / proposed** — containment, remediation, and steps to
   mitigate adverse effects.

Where you don't yet have a number, give a **range and your basis** ("approx.
X–Y vendor contact records across 1 tenant, derived from the audit trail"). State
clearly what's still under investigation and commit to a follow-up.

**Multi-jurisdiction**: notify your single lead authority (one-stop-shop) if you
have an EU main establishment. If you have **no EU establishment**, there's no
one-stop-shop — you may need to notify the authority in *each* member state where
affected individuals are. Counsel makes this call.

## 5. Evidence + record-keeping (Art. 33(5))

You must document **every** breach — notifiable or not — facts, effects, and
remedial action, sufficient for the authority to verify compliance.

- **The audit-log WORM store is your primary evidence.** Every status
  transition, approval, payment, vendor change, and sensitive read writes an
  append-only `audit_log` row; a DB trigger rejects all DELETE and any UPDATE
  other than the shipper's `shipped_at` stamp, and the shipper copies rows to
  WORM sinks (CloudWatch Logs + S3 Object Lock, Compliance mode). See
  [`../../backend/docs/audit-log-shipping.md`](../../backend/docs/audit-log-shipping.md).
  This means the trail of who-accessed-what is **tamper-evident** — pull it early
  and cite it in your assessment of scope.
- Note: audit rows record field **names** accessed, never values — so the trail
  shows *that* a bank field was read without itself leaking the number.
- Keep the incident doc, the notice(s) sent, authority correspondence, and the
  post-incident review (§ 8) together. Retain per your records-management policy
  ([`../../backend/docs/retention.md`](../../backend/docs/retention.md)).

## 6. Notifying data subjects (Art. 34 — only on HIGH risk)

If the breach is likely to result in a **high risk** to individuals, notify the
affected individuals **without undue delay**, in **clear and plain language**,
covering:

- the nature of the breach,
- the DPO/contact point,
- the likely consequences,
- the measures taken and recommended steps the individual can take (e.g. watch
  for fraud, rotate any reused password).

**Exceptions** (Art. 34(3)) — you may be able to skip individual notice if:
appropriate technical protection (e.g. strong encryption) made the data
unintelligible to the attacker; you've since taken measures ensuring the high
risk is no longer likely; or individual notice would require disproportionate
effort (then do a public communication instead). Document which exception you're
relying on and why.

For a multi-tenant breach, coordinate with the affected **customer (controller)**
— in many cases the *controller* notifies its own data subjects and you (the
processor) support them. The DPA defines who does what; default to supporting the
customer and giving them the facts fast (Art. 33(2): a processor notifies its
controller "without undue delay" — there is no 72h grace for the
processor→controller leg, so tell affected customers immediately).

## 7. US / state considerations (CCPA + state breach laws)

GDPR is not the only regime. If affected individuals are in the US:

- **State breach-notification laws** (all 50 states) — most require notice to
  affected residents "without unreasonable delay", and many require notice to the
  **state Attorney General** above a resident-count threshold (e.g. CA at 500+).
  Triggers vary but generally key on unencrypted personal information +
  identifiers (name + SSN/financial-account number — both of which this app can
  hold).
- **CCPA/CPRA** — creates a private right of action for breaches of certain
  unencrypted personal information due to failure to maintain reasonable
  security; reinforces the encryption/redaction mitigants in § 2.
- Notify your **cyber-insurer** (§ 0) — US breach response is where the policy's
  breach-counsel and notification-vendor benefits matter most.

Counsel maps the exact state obligations from the affected-resident list. Don't
guess thresholds.

## 8. Post-incident review (within ~1 week of resolution)

Don't close the incident at containment. Run a blameless post-mortem:

1. **Timeline** — detection → awareness → containment → notification →
   resolution, with the 72h clock and any § 3a 24h clocks annotated. Did we hit the deadline? If not,
   why, and what changes the next time?
2. **Root cause** — the actual vulnerability, fixed at source (not masked).
3. **Scope, confirmed** — final data-subject / record counts from the audit
   trail; file any follow-up notice that revises the initial estimate.
4. **Control gaps + actions** — concrete, owned, dated follow-ups (rail 6: drive
   each finding to resolution, don't leave it "noted"). Feed systemic gaps into
   the SOC 2 risk register ([`soc2-vendor.md`](./soc2-vendor.md)).
5. **Runbook update** — if this runbook (or the contacts in § 0) was wrong or
   stale, fix it in the same review.

---

## Quick checklist (print this)

- [ ] **T+0**: awareness timestamp recorded (UTC) — 72h clock and the 24h platform clocks started
- [ ] Incident record opened; Incident + Technical + Privacy leads assigned
- [ ] Cyber-insurer breach hotline called (may gate further steps)
- [ ] Audit trail pulled + preserved (WORM — tamper-evident scope evidence)
- [ ] Personal-data-breach? + risk-to-rights? + HIGH-risk? assessed and documented
- [ ] **Connected-platform notices sent within 24h** where our app is involved (§ 3a: Xero, Intuit, Shopify; Lightspeed immediately; Sage, Microsoft, Merge.dev promptly); tenants on their own app told their clock is running
- [ ] Containment done (access cut, credential rotated, root cause patched)
- [ ] Affected **customers (controllers)** notified without undue delay (no 72h grace)
- [ ] **Authority notified within 72h** (partial if needed; reasons logged if late)
- [ ] **Data subjects notified** if HIGH risk (Art. 34), or exception documented
- [ ] US state-AG / CCPA obligations checked with counsel (if US residents)
- [ ] Art. 33(5) record complete (notifiable or not)
- [ ] Post-incident review held; actions owned + dated; runbook updated

Related: [`README.md`](./README.md) (runbook index),
[`support-and-status.md`](./support-and-status.md) (operational incidents +
on-call), [`insurance.md`](./insurance.md) (cyber/E&O),
[`../sub-processors.md`](../sub-processors.md) (who else holds the data),
[`../../backend/docs/erp-integration.md`](../../backend/docs/erp-integration.md)
and [`../../backend/docs/commerce-integration.md`](../../backend/docs/commerce-integration.md)
(which platforms we connect to, § 3a),
[`../secrets-rotation.md`](../secrets-rotation.md) (credential rotation),
[`../../backend/docs/audit-log-shipping.md`](../../backend/docs/audit-log-shipping.md)
(WORM evidence).
