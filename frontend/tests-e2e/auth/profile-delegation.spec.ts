import {
  expect,
  test,
  authedTenantHeaders,
  createUserWithKnownPassword,
} from "../fixtures/helpers";
import { API_BASE } from "../fixtures/env";

/**
 * `/profile?section=delegation` — Out of office (decisions §511).
 *
 * The caller's own approval delegation: `GET/POST/DELETE /api/auth/delegation`.
 * The delegate list is `GET /api/auth/delegation/candidates`, which offers
 * exactly who the write accepts — an active colleague holding
 * `invoice.approve` — so an ap_clerk created alongside is never offered.
 *
 * The worker's storage state is its tenant admin, whose delegation is a
 * control-plane row that outlives the test, so every test clears it on the way
 * out through the endpoint itself.
 */
test.describe("/profile — out of office", () => {
  test.afterEach(async ({ page }) => {
    const resp = await page.request.delete(`${API_BASE}/api/auth/delegation`, {
      headers: await authedTenantHeaders(page),
    });
    expect(resp.status()).toBe(204);
  });

  test("turn on, read back, and end a delegation", async ({ page }) => {
    // Created before the page loads, so the candidate list includes them.
    await page.goto("/profile?section=delegation");
    const approver = await createUserWithKnownPassword(page, {
      mustChange: false,
      emailPrefix: "e2e-deleg-approver",
      roleNames: ["ap_manager"],
    });
    const clerk = await createUserWithKnownPassword(page, {
      mustChange: false,
      emailPrefix: "e2e-deleg-clerk",
      roleNames: ["ap_clerk"],
    });
    await page.reload();

    const panel = page.getByTestId("delegation-panel");
    await expect(panel.getByTestId("delegation-status")).toHaveText(
      "Off: invoices for your approval come to you.",
    );

    const select = panel.getByLabel("Delegate");
    await expect(select.locator(`option[value="${approver.id}"]`)).toHaveCount(
      1,
    );
    // A clerk cannot approve, so routing approvals to them would strand them.
    await expect(select.locator(`option[value="${clerk.id}"]`)).toHaveCount(0);

    // Saving with nothing chosen names both missing fields, each tied to
    // its control.
    await panel.getByTestId("delegation-save").click();
    await expect(select).toHaveAttribute("aria-invalid", "true");
    await expect(panel.getByLabel("Last day away")).toHaveAttribute(
      "aria-invalid",
      "true",
    );

    await select.selectOption(approver.id);
    const lastDay = new Date();
    lastDay.setDate(lastDay.getDate() + 3);
    const iso = `${lastDay.getFullYear()}-${String(lastDay.getMonth() + 1).padStart(2, "0")}-${String(lastDay.getDate()).padStart(2, "0")}`;
    await panel.getByLabel("Last day away").fill(iso);
    await panel.getByTestId("delegation-save").click();

    await expect(panel.getByTestId("delegation-status")).toContainText(
      "On: invoices for your approval go to Password Test through",
    );

    // The server holds it: a fresh load reads the same delegation back.
    await page.reload();
    await expect(panel.getByTestId("delegation-status")).toContainText("On:");
    await expect(panel.getByLabel("Delegate")).toHaveValue(approver.id);
    await expect(panel.getByLabel("Last day away")).toHaveValue(iso);

    await panel.getByTestId("delegation-end").click();
    await expect(panel.getByTestId("delegation-status")).toHaveText(
      "Off: invoices for your approval come to you.",
    );
    const after = await page.request.get(`${API_BASE}/api/auth/delegation`, {
      headers: await authedTenantHeaders(page),
    });
    expect((await after.json()).delegate_to_id).toBeNull();
  });

  test("the write refuses a delegate who cannot approve, with a coded reason", async ({
    page,
  }) => {
    await page.goto("/profile?section=delegation");
    const clerk = await createUserWithKnownPassword(page, {
      mustChange: false,
      emailPrefix: "e2e-deleg-clerk",
      roleNames: ["ap_clerk"],
    });
    const tomorrow = new Date(Date.now() + 2 * 24 * 3600 * 1000).toISOString();
    const resp = await page.request.post(`${API_BASE}/api/auth/delegation`, {
      headers: await authedTenantHeaders(page),
      data: { delegate_to_id: clerk.id, until: tomorrow },
    });
    expect(resp.status()).toBe(422);
    expect((await resp.json()).detail.code).toBe(
      "delegation_delegate_cannot_approve",
    );
  });
});
