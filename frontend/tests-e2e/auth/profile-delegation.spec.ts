import type { Page } from "@playwright/test";
import {
  expect,
  test,
  authedTenantHeaders,
  createUserWithKnownPassword,
  signInAndWait,
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

    // An approver (the worker's admin holds `invoice.approve`) is offered it.
    await expect(
      page.getByRole("link", { name: "Out of office" }),
    ).toHaveAttribute("aria-current", "page");

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

type DelegationBody = {
  delegate_to_id: string | null;
  delegate_to_name: string | null;
  until: string | null;
  is_active: boolean;
};
const OFF: DelegationBody = {
  delegate_to_id: null,
  delegate_to_name: null,
  until: null,
  is_active: false,
};

/** Stub the panel's two reads (nothing reaches the worker's real row). */
async function stubDelegation(
  page: Page,
  opts: {
    current?: DelegationBody | (() => { status: number; json?: unknown });
    candidates?: { id: string; full_name: string }[];
    onDelete?: () => void;
  },
) {
  await page.route(
    (url) => url.pathname === "/api/auth/delegation",
    async (route) => {
      const method = route.request().method();
      if (method === "DELETE") {
        opts.onDelete?.();
        return route.fulfill({ status: 204 });
      }
      if (method !== "GET") return route.fallback();
      const c = opts.current ?? OFF;
      if (typeof c === "function") {
        const r = c();
        return route.fulfill({ status: r.status, json: r.json ?? {} });
      }
      return route.fulfill({ json: c });
    },
  );
  await page.route(
    (url) => url.pathname === "/api/auth/delegation/candidates",
    (route) => route.fulfill({ json: opts.candidates ?? [] }),
  );
}

test.describe("/profile — out of office, page states (stubbed)", () => {
  test("a failed load says so in place and Retry recovers", async ({ page }) => {
    let fail = true;
    await stubDelegation(page, {
      current: () =>
        fail ? { status: 500, json: { detail: "boom" } } : { status: 200, json: OFF },
      candidates: [{ id: "c-1", full_name: "Casey Approver" }],
    });
    await page.goto("/profile?section=delegation");
    const panel = page.getByTestId("delegation-panel");
    const error = panel.getByTestId("delegation-load-error");
    await expect(error).toContainText("Couldn't load your out-of-office setting.");

    fail = false;
    await error.getByRole("button", { name: "Retry" }).click();
    await expect(panel.getByTestId("delegation-status")).toHaveText(
      "Off: invoices for your approval come to you.",
    );
  });

  test("a 403 reads as approvers-only, not as a failure", async ({ page }) => {
    await stubDelegation(page, {
      current: () => ({ status: 403, json: { detail: "Forbidden" } }),
    });
    await page.goto("/profile?section=delegation");
    const panel = page.getByTestId("delegation-panel");
    await expect(panel.getByTestId("delegation-approvers-only")).toBeVisible();
    await expect(panel.getByTestId("delegation-load-error")).toHaveCount(0);
  });

  test("no candidates: says so, and an ended delegation can still be cleared", async ({
    page,
  }) => {
    let deleted = false;
    await stubDelegation(page, {
      current: {
        delegate_to_id: "gone-1",
        delegate_to_name: "Former Approver",
        until: "2020-01-02T00:00:00Z",
        is_active: false,
      },
      candidates: [],
      onDelete: () => (deleted = true),
    });
    await page.goto("/profile?section=delegation");
    const panel = page.getByTestId("delegation-panel");
    await expect(panel.getByTestId("delegation-no-candidates")).toBeVisible();
    await expect(panel.getByTestId("delegation-status")).toContainText(
      "Former Approver",
    );
    await expect(panel.locator("form")).toHaveCount(0);

    await panel.getByRole("button", { name: "Clear the ended delegation" }).click();
    await expect(panel.getByTestId("delegation-status")).toHaveText(
      "Off: invoices for your approval come to you.",
    );
    // Focus lands on the status line, not <body>.
    await expect(panel.getByTestId("delegation-status")).toBeFocused();
    expect(deleted).toBe(true);
  });

  test("an active delegate no longer offered is still shown selected", async ({
    page,
  }) => {
    const until = new Date(Date.now() + 5 * 24 * 3600 * 1000).toISOString();
    await stubDelegation(page, {
      current: {
        delegate_to_id: "d-unlisted",
        delegate_to_name: "Dana Unlisted",
        until,
        is_active: true,
      },
      candidates: [{ id: "c-1", full_name: "Casey Approver" }],
    });
    await page.goto("/profile?section=delegation");
    const select = page.getByTestId("delegation-panel").getByLabel("Delegate");
    await expect(select).toHaveValue("d-unlisted");
    await expect(select.locator("option:checked")).toHaveText("Dana Unlisted");
  });

  test("an incomplete submit focuses the first invalid field, error first", async ({
    page,
  }) => {
    await stubDelegation(page, {
      candidates: [{ id: "c-1", full_name: "Casey Approver" }],
    });
    await page.goto("/profile?section=delegation");
    const panel = page.getByTestId("delegation-panel");
    const select = panel.getByLabel("Delegate");
    const lastDay = panel.getByLabel("Last day away");

    await panel.getByTestId("delegation-save").click();
    await expect(select).toBeFocused();
    await expect(select).toHaveAttribute(
      "aria-describedby",
      "delegation-delegate-error",
    );
    await expect(select).toHaveAccessibleDescription("Choose a delegate.");
    await expect(lastDay).toHaveAttribute(
      "aria-describedby",
      "delegation-last-day-error delegation-last-day-hint",
    );

    // With a delegate chosen, the day is now the first thing to fix.
    await select.selectOption("c-1");
    await panel.getByTestId("delegation-save").click();
    await expect(lastDay).toBeFocused();
    await expect(select).not.toHaveAttribute("aria-invalid", "true");
  });
});

test.describe("/profile — out of office is an approver's section", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("a clerk is not offered it, even by URL", async ({ page, tenantClerk }) => {
    await signInAndWait(page, tenantClerk);
    await page.goto("/profile?section=delegation");
    // The page falls back to its default section rather than an empty panel.
    await expect(
      page.getByRole("heading", { level: 2, name: "Account" }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "Out of office" })).toHaveCount(0);
    await expect(page.getByTestId("delegation-panel")).toHaveCount(0);
  });
});
