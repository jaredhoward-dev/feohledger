import { describe, expect, it } from "vitest";
import { orphanStateFrom } from "./erpOrphanBill.ts";

describe("orphanStateFrom", () => {
  const bill = {
    erp_document_id: "77",
    erp_document_number: "QB-1",
    amount: "1234.56",
    currency: "USD",
    erp_status: "open",
  };
  it("names a found bill", () => {
    expect(
      orphanStateFrom({ invoice_id: "i", found: true, bill }, "x"),
    ).toEqual({
      kind: "found",
      bill,
    });
  });
  it("says when the ERP holds none", () => {
    expect(
      orphanStateFrom({ invoice_id: "i", found: false, bill: null }, "x"),
    ).toEqual({
      kind: "none",
    });
  });
  it("shows a coded refusal beside the control, an uncoded failure as an error", () => {
    const coded = Object.assign(new Error("Cannot search"), {
      code: "erp_lookup_unsupported",
    });
    expect(orphanStateFrom(coded, "x")).toEqual({
      kind: "refused",
      message: "Cannot search",
    });
    expect(
      orphanStateFrom(Object.assign(new Error(""), { code: null }), "fallback"),
    ).toEqual({
      kind: "error",
      message: "fallback",
    });
    expect(orphanStateFrom(new Error("boom"), "fallback")).toEqual({
      kind: "error",
      message: "boom",
    });
  });
});
