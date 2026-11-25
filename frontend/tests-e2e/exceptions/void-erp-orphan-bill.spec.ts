import type { Page } from "@playwright/test";
import { en } from "#lib/i18n/locales/en.ts";
import { enRefusal } from "#lib/i18n/locales/refusal/en.ts";
import { expect, test } from "../fixtures/helpers";
import { resolveDialog } from "./dialogs";
import { exceptionSummary } from "./summary";

/**
 * `/exceptions` — the bill a failed push may have left in the ERP
 * (decisions §556).
 *
 * A duplicate flag on an invoice that names no bill (`erp_void_no_bill`) shows
 * the shared lookup panel under the flag's void: "Find it in the ERP" calls
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
  };
}

async function stubFlag(page: Page) {
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
          items: [row()],
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
        body: JSON.stringify(row()),
      }),
  );
}

test.describe("/exceptions — the bill a failed push left in the ERP", () => {
  test("finds the bill, then voids it with a reason", async ({ page }) => {
    await stubFlag(page);
    await page.route(`**/api/invoices/${INVOICE}/erp-orphan-bill`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          invoice_id: INVOICE,
          found: true,
          bill: {
            erp_document_id: "77",
            erp_document_number: "QB-77",
            amount: "100.00",
            currency: "USD",
            erp_status: "open",
          },
        }),
      }),
    );
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
    await modal.getByTestId("exception-erp-orphan-void-open").click();
    const confirm = page
      .getByRole("dialog")
      .filter({ has: page.getByTestId("exception-erp-orphan-void-form") });
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
      page
        .getByTestId("toast")
        .filter({
          hasText: en["invoices.modal.erpOrphan.done"].replace(
            "{number}",
            "QB-77",
          ),
        }),
    ).toBeVisible();
  });

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
