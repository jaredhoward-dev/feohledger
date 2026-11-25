import type { Page } from "@playwright/test";
import { en } from "#lib/i18n/locales/en.ts";
import { enRefusal } from "#lib/i18n/locales/refusal/en.ts";
import { expectNoA11yViolations } from "../a11y/axe-helper";
import {
  API_BASE,
  authedTenantHeaders,
  deleteInvoicesWhere,
  expect,
  tenantPsql,
  test,
} from "../fixtures/helpers";
import { orphanVoidDialog, resolveDialog } from "./dialogs";
import { exceptionSummary } from "./summary";

/**
 * `/exceptions` — the bill a failed push may have left in the ERP
 * (decisions §556).
 *
 * A duplicate flag whose invoice is a failed push that may have left a bill
 * carries the server's `erp_orphan_lookup_refusal` verdict (decisions §560)
 * and shows the shared lookup panel under the flag's void; a flag on a
 * never-pushed invoice carries none and shows no panel. "Find it in the ERP" calls
 * `GET /api/invoices/{id}/erp-orphan-bill`; a found bill offers "Void this
 * bill…", whose confirm needs a reason and posts the bill's id with it to
 * `POST /api/invoices/{id}/void-erp-orphan-bill`. A coded refusal is shown
 * beside the control in the reader's language. Stubbed so the answers are
 * exact.
 */

const ID = "00000000-0000-4000-9556-000000000001";
const INVOICE = "00000000-0000-4000-9556-0000000000aa";
const NO_BILL = { code: "erp_void_no_bill", message: "No bill.", params: {} };

function row() {
  return {
    id: ID,
    invoice_id: INVOICE,
    invoice_number: "E2E-ORPH-1",
    vendor_name: "Orphan Vendor",
    amount: "100.00",
    currency: "USD",
    exception_type: "duplicate",
    type_label: "Duplicate",
    severity: "error",
    description: "stubbed",
    status: "open",
    resolution: null,
    resolved_by: null,
    resolved_at: null,
    assigned_to: null,
    assigned_to_user_id: null,
    due_at: null,
    is_overdue: false,
    time_to_resolution_hours: null,
    created_at: "2026-01-01T00:00:00Z",
    decision_refusals: { resolve: null, dismiss: null, escalate: null },
    void_erp_bill_refusal: NO_BILL,
    erp_orphan_lookup_refusal: null as null | undefined,
  };
}

type Row = ReturnType<typeof row>;

const BILL = {
  erp_document_id: "77",
  erp_document_number: "QB-77",
  amount: "100.00",
  currency: "USD",
  erp_status: "open",
};

async function stubFound(page: Page) {
  await page.route(`**/api/invoices/${INVOICE}/erp-orphan-bill`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ invoice_id: INVOICE, found: true, bill: BILL }),
    }),
  );
}

async function stubFlag(page: Page, flag: Row = row()) {
  await page.route("**/api/exceptions/summary*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        exceptionSummary({
          open: 1,
          by_type: { duplicate: 1 },
          by_severity: { error: 1 },
        }),
      ),
    }),
  );
  await page.route(
    (url) => url.pathname === "/api/exceptions",
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [flag],
          total: 1,
          page: 1,
          page_size: 20,
        }),
      }),
  );
  await page.route(
    (url) => url.pathname === `/api/exceptions/${ID}`,
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(flag),
      }),
  );
}

test.describe("/exceptions — the bill a failed push left in the ERP", () => {
  test("finds the bill, then voids it with a reason", async ({ page }) => {
    await stubFlag(page);
    await stubFound(page);
    let posted: unknown = null;
    await page.route(
      `**/api/invoices/${INVOICE}/void-erp-orphan-bill`,
      async (route) => {
        posted = route.request().postDataJSON();
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            invoice_id: INVOICE,
            erp_document_id: "77",
            invoice_status: "failed",
            replayed: false,
          }),
        });
      },
    );
    await page.goto(`/exceptions?id=${ID}`);
    const modal = resolveDialog(page);
    await modal.getByTestId("exception-erp-orphan-find").click();
    await expect(modal.getByTestId("exception-erp-orphan-found")).toBeVisible();
    // The ERP's raw status as a code value; the id beside the number only
    // because the ERP gave a separate number.
    await expect(
      modal.getByTestId("exception-erp-orphan-erp-status"),
    ).toHaveText("open");
    await expect(modal.getByTestId("exception-erp-orphan-erp-id")).toHaveText(
      "77",
    );
    // No Retry button here: the hint points to the invoice instead.
    await expect(
      modal.getByTestId("exception-erp-orphan-open-invoice"),
    ).toHaveAttribute("href", `/invoices?id=${INVOICE}`);
    await modal.getByTestId("exception-erp-orphan-void-open").click();
    const confirm = orphanVoidDialog(page);
    // Escape on the stacked confirm closes only it.
    await expect(confirm).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(confirm).toBeHidden();
    await expect(modal).toBeVisible();
    await modal.getByTestId("exception-erp-orphan-void-open").click();
    await expect(
      confirm.getByTestId("exception-erp-orphan-void-confirm"),
    ).toBeDisabled();
    await confirm
      .getByTestId("exception-erp-orphan-void-reason")
      .fill("Duplicate of INV-1");
    await confirm.getByTestId("exception-erp-orphan-void-confirm").click();
    await expect(confirm).toBeHidden();
    expect(posted).toEqual({
      erp_document_id: "77",
      reason: "Duplicate of INV-1",
    });
    await expect(
      page.getByTestId("toast").filter({
        hasText: en["invoices.modal.erpOrphan.done"].replace(
          "{number}",
          "QB-77",
        ),
      }),
    ).toBeVisible();
    // The void removed its trigger: focus lands on Find in the host dialog,
    // and the panel keeps saying what happened until the invoice reloads.
    await expect(modal.getByTestId("exception-erp-orphan-find")).toBeFocused();
    await expect(modal.getByTestId("exception-erp-orphan-voided")).toHaveText(
      en["invoices.modal.erpOrphan.voided"]
        .replace("{number}", "QB-77")
        .replace("{retry}", en["invoices.modal.erp.retrySend"]),
    );
    await expect(
      modal.getByTestId("exception-erp-orphan-status"),
    ).toHaveAttribute("role", "status");
  });

  test("an uncoded failure is an alert, a refused void keeps the confirm open", async ({
    page,
  }) => {
    await stubFlag(page);
    let lookups = 0;
    await page.route(`**/api/invoices/${INVOICE}/erp-orphan-bill`, (route) => {
      lookups += 1;
      return lookups === 1
        ? route.fulfill({
            status: 500,
            contentType: "application/json",
            body: JSON.stringify({ detail: "Internal Server Error" }),
          })
        : route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              invoice_id: INVOICE,
              found: true,
              bill: BILL,
            }),
          });
    });
    await page.route(
      `**/api/invoices/${INVOICE}/void-erp-orphan-bill`,
      (route) =>
        route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              code: "erp_void_not_found",
              message: "The ERP no longer names this bill.",
              params: {},
            },
          }),
        }),
    );
    await page.goto(`/exceptions?id=${ID}`);
    const modal = resolveDialog(page);
    await modal.getByTestId("exception-erp-orphan-find").click();
    const error = modal.getByTestId("exception-erp-orphan-error");
    await expect(error).toBeVisible();
    await expect(error).toHaveAttribute("role", "alert");
    await expect(modal.getByTestId("exception-erp-orphan-refusal")).toHaveCount(
      0,
    );

    await modal.getByTestId("exception-erp-orphan-find").click();
    await expect(error).toHaveCount(0);
    await modal.getByTestId("exception-erp-orphan-void-open").click();
    const confirm = orphanVoidDialog(page);
    await confirm.getByTestId("exception-erp-orphan-void-reason").fill("dup");
    await confirm.getByTestId("exception-erp-orphan-void-confirm").click();
    await expect(
      confirm.getByTestId("exception-erp-orphan-void-error"),
    ).toBeVisible();
    await expect(confirm).toBeVisible();
    await expect(modal.getByTestId("exception-erp-orphan-voided")).toHaveCount(
      0,
    );
  });

  test("a duplicate flag on a never-pushed invoice shows no lookup panel", async ({
    page,
  }) => {
    // No verdict on the row: the invoice is not a failed push, so the server
    // would refuse the lookup (`erp_lookup_not_failed`) and nothing is offered.
    const flag = row();
    flag.erp_orphan_lookup_refusal = undefined;
    await stubFlag(page, flag);
    await page.goto(`/exceptions?id=${ID}`);
    const modal = resolveDialog(page);
    await expect(modal.getByTestId("erp-void-panel")).toBeVisible();
    await expect(modal.getByTestId("exception-erp-orphan")).toHaveCount(0);
  });

  for (const { name, width, height } of [
    { name: "desktop", width: 1280, height: 800 },
    { name: "phone (320px)", width: 320, height: 720 },
  ] as const) {
    test(`the open panel has no axe violations at ${name}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height });
      await stubFlag(page);
      await stubFound(page);
      await page.goto(`/exceptions?id=${ID}`);
      const modal = resolveDialog(page);
      await modal.getByTestId("exception-erp-orphan-find").click();
      await expect(
        modal.getByTestId("exception-erp-orphan-found"),
      ).toBeVisible();
      await expectNoA11yViolations(page);
    });
  }

  test("says when the ERP holds none, and shows a coded refusal", async ({
    page,
  }) => {
    await stubFlag(page);
    let calls = 0;
    await page.route(`**/api/invoices/${INVOICE}/erp-orphan-bill`, (route) => {
      calls += 1;
      return calls === 1
        ? route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              invoice_id: INVOICE,
              found: false,
              bill: null,
            }),
          })
        : route.fulfill({
            status: 409,
            contentType: "application/json",
            body: JSON.stringify({
              detail: {
                code: "erp_lookup_unsupported",
                message: "x",
                params: {},
              },
            }),
          });
    });
    await page.goto(`/exceptions?id=${ID}`);
    const modal = resolveDialog(page);
    await modal.getByTestId("exception-erp-orphan-find").click();
    await expect(modal.getByTestId("exception-erp-orphan-none")).toHaveText(
      en["invoices.modal.erpOrphan.none"],
    );
    await modal.getByTestId("exception-erp-orphan-find").click();
    await expect(modal.getByTestId("exception-erp-orphan-refusal")).toHaveText(
      enRefusal["refusal.erpLookupUnsupported"],
    );
    await expect(
      modal.getByTestId("exception-erp-orphan-void-open"),
    ).toHaveCount(0);
  });
});

const PREFIX = "E2E-ORPHAN-MODAL";

test.describe("invoice modal — the bill a failed push left in the ERP", () => {
  test.afterEach(() =>
    deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`),
  );

  test("a failed push that may have posted offers the lookup from the served verdict", async ({
    page,
  }) => {
    const number = `${PREFIX}-${Date.now()}`;
    const res = await page.request.post(`${API_BASE}/api/invoices`, {
      headers: await authedTenantHeaders(page),
      data: {
        vendor: "E2E Orphan Vendor",
        invoice_number: number,
        amount: "100.00",
        currency: "USD",
      },
    });
    expect(res.status()).toBe(201);
    const id = ((await res.json()) as { id: string }).id;
    // The stage is the fixture: a push that failed with its outcome unknown,
    // and the failure the ERP section reads.
    tenantPsql(
      `UPDATE invoices SET status='failed', approved_by='Approver', approval_date=CURRENT_DATE WHERE id='${id}';` +
        ` UPDATE workflow_instances SET state_data = COALESCE(state_data, '{}'::jsonb) || '{"erp_outcome_unknown": true}'::jsonb WHERE invoice_id='${id}';` +
        ` INSERT INTO audit_log (id, correlation_id, organization_id, action, entity_type, entity_id, details, created_at)` +
        ` SELECT gen_random_uuid(), correlation_id, organization_id, 'invoice.erp_failed', 'invoice', id, '{"error": "timeout"}'::jsonb, now() FROM invoices WHERE id='${id}'`,
    );
    await page.route(`**/api/invoices/${id}/erp-orphan-bill`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          invoice_id: id,
          found: true,
          bill: { ...BILL, erp_document_number: null },
        }),
      }),
    );
    const verdict = page.waitForResponse(
      (r) =>
        r.url().endsWith(`/api/invoices/${id}/review-actions`) &&
        r.status() === 200,
    );
    await page.goto(`/invoices?id=${id}`);
    const served = (await (await verdict).json()) as Record<string, unknown>;
    expect(served.erp_orphan_lookup_refusal).toBeNull();
    const modal = page.locator('div.modal[role="dialog"]');
    const panel = modal.getByTestId("invoice-erp-orphan");
    await expect(panel).toBeVisible();
    await panel.getByTestId("invoice-erp-orphan-find").click();
    // No separate number: the id is named once, not twice.
    await expect(panel.getByTestId("invoice-erp-orphan-found")).toContainText(
      "77",
    );
    await expect(panel.getByTestId("invoice-erp-orphan-erp-id")).toHaveCount(0);
    // The hint names the Retry button as this locale labels it.
    await expect(panel).toContainText(
      en["invoices.modal.erpOrphan.keepHint"].replace(
        "{retry}",
        en["invoices.modal.erp.retrySend"],
      ),
    );
  });
});
