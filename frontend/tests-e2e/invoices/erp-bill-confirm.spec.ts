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

test.describe("invoice modal — confirm the bill on a shared Xero key", () => {
  test.afterEach(() =>
    deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`),
  );

  test("finds the bill and confirms it with a reason; a claimed bill is refused in the dialog", async ({
    page,
  }) => {
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
        ` UPDATE workflow_instances SET state_data = COALESCE(state_data, '{}'::jsonb) || '{"erp_last_refusal_code": "erp_bill_key_shared"}'::jsonb WHERE invoice_id='${id}'`,
    );
    await page.route(`**/api/invoices/${id}/review-actions`, async (route) => {
      const real = await route.fetch();
      const body = (await real.json()) as Record<string, unknown>;
      await route.fulfill({
        response: real,
        json: { ...body, erp_bill_confirm_refusal: null },
      });
    });
    await page.route(`**/api/invoices/${id}/erp-bill-confirmation`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ invoice_id: id, found: true, bill: BILL }),
      }),
    );
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

    await page.goto(`/invoices?id=${id}`);
    const modal = page.locator('div.modal[role="dialog"]').first();
    const panel = modal.getByTestId("invoice-erp-bill-confirm");
    await expect(panel).toBeVisible();
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
});
