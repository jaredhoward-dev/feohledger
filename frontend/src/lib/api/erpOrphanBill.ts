/**
 * The bill a failed ERP push may have left in the ERP (decisions §556):
 * `GET /api/invoices/{id}/erp-orphan-bill` names it by the adapter's read-only
 * idempotency lookup, `POST /api/invoices/{id}/void-erp-orphan-bill` voids it
 * through the flag void's shared flow. Both gate on `payment.void`. Whether
 * to show the panel is the server's verdict `erp_orphan_lookup_refusal`
 * (decisions §560), never a rule restated here.
 */
/** The bill the lookup named: the ERP's ids, its total, where it stands. */
export interface OrphanBill {
  erp_document_id: string;
  erp_document_number: string | null;
  /** Exact decimal string, never a float. */
  amount: string | null;
  currency: string | null;
  erp_status: string;
}

export interface OrphanLookup {
  invoice_id: string;
  found: boolean;
  bill: OrphanBill | null;
}

/** The four async states of the lookup, plus a coded refusal the server sent. */
export type OrphanState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "refused"; message: string }
  | { kind: "error"; message: string }
  | { kind: "none" }
  | { kind: "found"; bill: OrphanBill };

/** The state a lookup answer (or its error) puts the panel in. A coded 409 /
 *  502 is a refusal shown beside the control; anything else is an error. */
export function orphanStateFrom(
  result: OrphanLookup | unknown,
  fallback: string,
): OrphanState {
  // Duck-typed on `ApiError.code` (`#lib/api.ts`), so this module stays free
  // of the API client's environment imports.
  if (result instanceof Error) {
    const code = (result as Error & { code?: unknown }).code;
    return typeof code === "string" && code
      ? { kind: "refused", message: result.message }
      : { kind: "error", message: result.message || fallback };
  }
  const lookup = result as OrphanLookup;
  if (lookup?.found && lookup.bill) return { kind: "found", bill: lookup.bill };
  return { kind: "none" };
}
