/**
 * The invoice modal's "This bill is this invoice's…" for a Xero push refused
 * `erp_bill_key_shared` (decisions §572, §587). Offered from the server's
 * verdict `erp_bill_confirm_refusal` alone; the lookup and the confirmation
 * are stubbed (a Xero connection is not part of the e2e tenant), the verdict
 * is the real review-actions answer with the key added, as the server serves
 * it for such a push.
 */
import { en } from "#lib/i18n/locales/en.ts";
import { enRefusal } from "#lib/i18n/locales/refusal/en.ts";
import type { Page, Route } from "@playwright/test";
import {
  API_BASE,
  authedTenantHeaders,
  deleteInvoicesWhere,
  expect,
  tenantPsql,
  test,
} from "../fixtures/helpers";

const PREFIX = "E2E-BILL-CONFIRM";
const BILL = {
  erp_document_id: "xero-bill-77",
  erp_document_number: "SUP-77",
  amount: "100.00",
  currency: "USD",
  erp_status: "open",
};

type Verdicts = Record<string, unknown>;

/** A failed push refused `erp_bill_key_shared`, with the verdicts the server
 *  serves for it (the real review-actions answer, `extra` merged on top). */
async function sharedKeyInvoice(
  page: Page,
  extra: Verdicts = { erp_bill_confirm_refusal: null },
): Promise<string> {
  const number = `${PREFIX}-${Date.now()}`;
  const res = await page.request.post(`${API_BASE}/api/invoices`, {
    headers: await authedTenantHeaders(page),
    data: {
      vendor: "E2E Confirm Vendor",
      invoice_number: number,
      amount: "100.00",
      currency: "USD",
    },
  });
  expect(res.status()).toBe(201);
  const id = ((await res.json()) as { id: string }).id;
  tenantPsql(
    `UPDATE invoices SET status='failed', approved_by='Approver', approval_date=CURRENT_DATE WHERE id='${id}';` +
      ` UPDATE workflow_instances SET state_data = COALESCE(state_data, '{}'::jsonb) || '{"erp_last_refusal_code": "erp_bill_key_shared"}'::jsonb WHERE invoice_id='${id}';` +
      // The row a refused push writes: the invoice's ERP section shows a
      // failed push from it, so the panel inside it renders.
      ` INSERT INTO audit_log (id, correlation_id, organization_id, action, entity_type, entity_id, details, created_at) SELECT gen_random_uuid(), correlation_id, organization_id, 'invoice.erp_failed', 'invoice', id, '{"error": "xero: another invoice shares this supplier and number", "refusal": {"code": "erp_bill_key_shared", "params": {"provider": "xero"}}}'::jsonb, now() FROM invoices WHERE id='${id}'`,
  );
  await page.route(`**/api/invoices/${id}/review-actions`, async (route) => {
    const real = await route.fetch();
    const body = (await real.json()) as Verdicts;
    await route.fulfill({ response: real, json: { ...body, ...extra } });
  });
  return id;
}

/** Answer the read-only lookup with `answer` (a body, or a status to fail). */
async function stubLookup(
  page: Page,
  id: string,
  answer: { found: boolean } | { status: number; detail: unknown },
): Promise<void> {
  await page.route(
    `**/api/invoices/${id}/erp-bill-confirmation`,
    (route: Route) =>
      "status" in answer
        ? route.fulfill({
            status: answer.status,
            contentType: "application/json",
            body: JSON.stringify({ detail: answer.detail }),
          })
        : route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              invoice_id: id,
              found: answer.found,
              bill: answer.found ? BILL : null,
            }),
          }),
  );
}

async function openPanel(page: Page, id: string) {
  const verdicts = page.waitForResponse((r) =>
    r.url().endsWith(`/api/invoices/${id}/review-actions`),
  );
  await page.goto(`/invoices?id=${id}`);
  await verdicts;
  const modal = page.locator('div.modal[role="dialog"]').first();
  const panel = modal.getByTestId("invoice-erp-bill-confirm");
  await expect(panel).toBeVisible();
  return { modal, panel };
}

test.describe("invoice modal — confirm the bill on a shared Xero key", () => {
  test.afterEach(() =>
    deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`),
  );

  test("finds the bill and confirms it with a reason; a claimed bill is refused in the dialog", async ({
    page,
  }) => {
    const id = await sharedKeyInvoice(page);
    await stubLookup(page, id, { found: true });
    let posts = 0;
    await page.route(`**/api/invoices/${id}/confirm-erp-bill`, (route) => {
      posts += 1;
      const sent = route.request().postDataJSON() as Record<string, string>;
      expect(sent).toEqual({
        erp_document_id: BILL.erp_document_id,
        reason: "Checked in Xero",
      });
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            code: "erp_bill_claimed",
            message: "Another invoice already holds this bill",
            params: {},
          },
        }),
      });
    });

    const { panel } = await openPanel(page, id);
    await panel.getByTestId("invoice-erp-bill-confirm-find").click();
    await expect(
      panel.getByTestId("invoice-erp-bill-confirm-found"),
    ).toContainText("SUP-77");
    await panel.getByTestId("invoice-erp-bill-confirm-open").click();

    const dialog = page.getByRole("dialog", {
      name: en["invoices.modal.erpBillConfirm.confirmTitle"],
    });
    const confirm = dialog.getByTestId("invoice-erp-bill-confirm-confirm");
    // A reason is required.
    await expect(confirm).toBeDisabled();
    await dialog
      .getByTestId("invoice-erp-bill-confirm-reason")
      .fill("  Checked in Xero ");
    await confirm.click();
    await expect(
      dialog.getByTestId("invoice-erp-bill-confirm-confirm-error"),
    ).toHaveText(enRefusal["refusal.erpBillClaimed"]);
    expect(posts).toBe(1);
  });

  test("a confirmation toasts, closes the invoice, refetches the list and puts focus in <main>", async ({
    page,
  }) => {
    const id = await sharedKeyInvoice(page);
    await stubLookup(page, id, { found: true });
    await page.route(`**/api/invoices/${id}/confirm-erp-bill`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id, status: "sending_to_erp" }),
      }),
    );

    const { modal, panel } = await openPanel(page, id);
    await panel.getByTestId("invoice-erp-bill-confirm-find").click();
    await panel.getByTestId("invoice-erp-bill-confirm-open").click();
    const dialog = page.getByRole("dialog", {
      name: en["invoices.modal.erpBillConfirm.confirmTitle"],
    });
    await dialog
      .getByTestId("invoice-erp-bill-confirm-reason")
      .fill("Checked in Xero");

    const refetch = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/api/invoices" &&
        r.request().method() === "GET",
    );
    await dialog.getByTestId("invoice-erp-bill-confirm-confirm").click();

    await expect(
      page.getByText(
        en["invoices.modal.erpBillConfirm.done"].replace("{number}", "SUP-77"),
      ),
    ).toBeVisible();
    await expect(dialog).toHaveCount(0);
    await expect(modal).toHaveCount(0);
    await refetch;
    // The invoice dialog was opened from a deep link, with nothing focused:
    // its close puts focus in <main>, not on <body>.
    await expect(page.locator("main")).toBeFocused();
  });

  test("a lookup that finds nothing says so in the live region", async ({
    page,
  }) => {
    const id = await sharedKeyInvoice(page);
    await stubLookup(page, id, { found: false });
    const { panel } = await openPanel(page, id);
    await panel.getByTestId("invoice-erp-bill-confirm-find").click();
    await expect(
      panel
        .getByTestId("invoice-erp-bill-confirm-status")
        .getByTestId("invoice-erp-bill-confirm-none"),
    ).toHaveText(en["invoices.modal.erpBillConfirm.none"]);
    await expect(panel.getByTestId("invoice-erp-bill-confirm-open")).toHaveCount(0);
  });

  test("an uncoded lookup failure is an alert, and Find stays usable", async ({
    page,
  }) => {
    const id = await sharedKeyInvoice(page);
    await stubLookup(page, id, { status: 500, detail: "boom" });
    const { panel } = await openPanel(page, id);
    const find = panel.getByTestId("invoice-erp-bill-confirm-find");
    await find.click();
    const error = panel.getByTestId("invoice-erp-bill-confirm-error");
    await expect(error).toHaveAttribute("role", "alert");
    await expect(error).not.toBeEmpty();
    await expect(find).toBeEnabled();
    await expect(panel.getByTestId("invoice-erp-bill-confirm-found")).toHaveCount(0);
  });

  test("a refused verdict disables Find and says why beside it", async ({
    page,
  }) => {
    const id = await sharedKeyInvoice(page, {
      erp_bill_confirm_refusal: {
        code: "erp_lookup_unsupported",
        message: "The connected ERP cannot be searched for this bill",
        params: {},
      },
    });
    let lookups = 0;
    await page.route(`**/api/invoices/${id}/erp-bill-confirmation`, (route) => {
      lookups += 1;
      return route.abort();
    });
    const { panel } = await openPanel(page, id);
    const find = panel.getByTestId("invoice-erp-bill-confirm-find");
    await expect(find).toBeDisabled();
    const note = panel.getByTestId("invoice-erp-bill-confirm-verdict");
    await expect(note).toHaveText(enRefusal["refusal.erpLookupUnsupported"]);
    await expect(find).toHaveAttribute(
      "aria-describedby",
      "invoice-erp-bill-confirm-verdict",
    );
    expect(lookups).toBe(0);
  });

  test("the orphan-bill panel stays hidden while the confirmation is offered", async ({
    page,
  }) => {
    // The shared-key push created no bill, so the orphan lookup would only
    // state its refusal beside a second, disabled "Find it in the ERP".
    const id = await sharedKeyInvoice(page, {
      erp_bill_confirm_refusal: null,
      erp_orphan_lookup_refusal: {
        code: "erp_lookup_unsupported",
        message: "The connected ERP cannot be searched for this bill",
        params: {},
      },
    });
    const { modal } = await openPanel(page, id);
    await expect(modal.getByTestId("invoice-erp-orphan")).toHaveCount(0);
    await expect(
      modal.getByRole("button", { name: en["invoices.modal.erpBillConfirm.find"] }),
    ).toHaveCount(1);
  });

  test("Escape closes the confirm dialog only, and focus returns to its opener", async ({
    page,
  }) => {
    const id = await sharedKeyInvoice(page);
    await stubLookup(page, id, { found: true });
    const { modal, panel } = await openPanel(page, id);
    await panel.getByTestId("invoice-erp-bill-confirm-find").click();
    const opener = panel.getByTestId("invoice-erp-bill-confirm-open");
    await opener.click();
    const dialog = page.getByRole("dialog", {
      name: en["invoices.modal.erpBillConfirm.confirmTitle"],
    });
    await expect(dialog.getByTestId("invoice-erp-bill-confirm-reason")).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(modal).toBeVisible();
    await expect(opener).toBeFocused();
  });
});
