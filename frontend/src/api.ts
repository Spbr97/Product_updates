// Typed client for the Product Entry endpoints under /api/v1.
//
// The UI owns no business rules -- it renders what the API returns and posts back what a
// person typed. Every shape here mirrors a Pydantic schema in
// src/product_tracker/api/schemas/product_entries.py; money fields arrive as decimal
// strings, not numbers, so they are kept as strings and formatted at the edge.

export type ProductEntryStatus = "active" | "archived";
export type Availability = "in_stock" | "out_of_stock" | "unavailable" | "unknown";
export type TrackingStatus = "active" | "paused";
export type CheckStatus = "success" | "failed" | "partial" | "skipped";

export interface ListingResponse {
  id: number;
  store: string;
  store_name: string;
  product_name: string;
  url: string;
  product_id: number;
  price: string | null;
  currency: string | null;
  availability: Availability;
  tracking_status: TrackingStatus;
  last_checked_at: string | null;
  last_check_status: CheckStatus | null;
  last_check_error: string | null;
  is_active: boolean;
  deactivated_at: string | null;
}

export interface ProductEntry {
  id: number;
  product_name: string;
  status: ProductEntryStatus;
  listings: ListingResponse[];
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ListingCheckResult {
  listing_id: number;
  store: string;
  status: CheckStatus;
  price: string | null;
  currency: string | null;
  availability: Availability | null;
  error_type: string | null;
  error_detail: string | null;
}

export interface EntryCheckResponse {
  product_entry_id: number;
  results: ListingCheckResult[];
}

export interface PricePoint {
  price: string;
  currency: string;
  observed_at: string;
}
export interface AvailabilityPoint {
  availability: Availability;
  observed_at: string;
}
export interface ListingHistory {
  listing_id: number;
  store: string;
  store_name: string;
  prices: PricePoint[];
  availability: AvailabilityPoint[];
}
export interface EntryHistory {
  product_entry_id: number;
  listings: ListingHistory[];
}

export interface ListingStats {
  listing_id: number;
  store: string;
  store_name: string;
  currency: string | null;
  observations: number;
  current: string | null;
  lowest: string | null;
  highest: string | null;
  average: string | null;
  lowest_at: string | null;
  first_observed_at: string | null;
  changed_by: string | null;
  mixed_currency: boolean;
}
export interface EntryStats {
  product_entry_id: number;
  listings: ListingStats[];
}

// --- Product groups and the comparison grid --------------------------------------
//
// A group is one product across several *models* (down the side) and several *shops*
// (across the top). The cell status is the field to branch on, never `price === null`:
// four very different situations produce a null price and only one of them says
// anything about the product.

export type CellStatus =
  | "ok"
  | "out_of_stock"
  | "no_price"
  | "blocked"
  | "failed"
  | "never_checked"
  | "not_tracked";

export interface VariantSummary {
  id: number;
  label: string;
  attributes: Record<string, string>;
}

export interface Group {
  id: number;
  slug: string;
  name: string;
  brand: string | null;
  notes: string | null;
  variants: VariantSummary[];
  created_at: string;
}

export interface ComparisonCell {
  status: CellStatus;
  price: string | null;
  currency: string | null;
  availability: Availability;
  product_id: number | null;
  url: string | null;
  last_checked_at: string | null;
  is_stale: boolean;
}

export interface ComparisonRow {
  variant_id: number | null;
  label: string;
  attributes: Record<string, string>;
  cells: Record<string, ComparisonCell>;
  best_price: string | null;
  best_stores: string[];
  spread: string | null;
}

export interface StoreColumn {
  slug: string;
  name: string;
}

export interface Comparison {
  group_slug: string;
  group_name: string;
  brand: string | null;
  stores: StoreColumn[];
  rows: ComparisonRow[];
  generated_at: string;
  currencies: string[];
  /** Prices are never converted, so a single "best" across currencies is not offered. */
  mixed_currency: boolean;
}

export interface ListingInput {
  product_name: string;
  url: string;
}
export interface CreatePayload {
  product_name: string;
  amazon: ListingInput;
  flipkart: ListingInput;
}

/** The one error shape the API ever returns. */
export class ApiError extends Error {
  readonly status: number;
  /** Stable machine code: "duplicate_listing", "invalid_store_url", "not_found", ... */
  readonly type: string;

  constructor(status: number, type: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
  }
}

const BASE = "/api/v1";

/**
 * The key, if a person has signed in. Held in localStorage rather than a cookie: the API
 * authenticates on the `X-API-Key` header only, and a header cannot be forged onto a
 * request by another origin the way a cookie is sent automatically. That is why there is
 * no cookie path -- it would be a CSRF surface bought for nothing.
 */
function apiKey(): string | null {
  try {
    return window.localStorage.getItem("pt_key");
  } catch {
    return null;
  }
}

export function hasApiKey(): boolean {
  return apiKey() !== null;
}

/**
 * Called when the API rejects our credentials, so the shell can ask for a key instead of
 * leaving each page to render "missing or invalid X-API-Key" at a dead end.
 *
 * A single hook rather than per-page handling: any page can be the first one loaded, and
 * four copies of this would drift.
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

export function setApiKey(key: string | null): void {
  try {
    if (key) window.localStorage.setItem("pt_key", key);
    else window.localStorage.removeItem("pt_key");
  } catch {
    /* private mode: the cookie path still works for reads on an open install */
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const key = apiKey();
  if (key) headers["X-API-Key"] = key;

  const res = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  const parsed = text ? JSON.parse(text) : null;

  if (!res.ok) {
    if (res.status === 401) onUnauthorized?.();
    const err = parsed?.error ?? {};
    throw new ApiError(
      res.status,
      err.type ?? "error",
      err.message ?? `Request failed (${res.status})`,
    );
  }
  return parsed as T;
}

export const api = {
  listEntries: (status: ProductEntryStatus = "active") =>
    request<Page<ProductEntry>>(
      "GET",
      `/product-entries?status=${status}&limit=100`,
    ),
  getEntry: (id: number) =>
    request<ProductEntry>("GET", `/product-entries/${id}`),
  createEntry: (payload: CreatePayload) =>
    request<ProductEntry>("POST", "/product-entries", payload),
  renameEntry: (id: number, canonical_name: string) =>
    request<ProductEntry>("PATCH", `/product-entries/${id}`, { canonical_name }),
  archiveEntry: (id: number) =>
    request<void>("DELETE", `/product-entries/${id}`),
  checkEntry: (id: number) =>
    request<EntryCheckResponse>("POST", `/product-entries/${id}/check`),
  checkListing: (id: number, listingId: number) =>
    request<EntryCheckResponse>(
      "POST",
      `/product-entries/${id}/listings/${listingId}/check`,
    ),
  pauseEntry: (id: number) =>
    request<ProductEntry>("POST", `/product-entries/${id}/pause`),
  resumeEntry: (id: number) =>
    request<ProductEntry>("POST", `/product-entries/${id}/resume`),
  updateListing: (
    id: number,
    listingId: number,
    patch: { product_name?: string; url?: string },
  ) =>
    request<ListingResponse>(
      "PATCH",
      `/product-entries/${id}/listings/${listingId}`,
      patch,
    ),
  deactivateListing: (id: number, listingId: number) =>
    request<void>("DELETE", `/product-entries/${id}/listings/${listingId}`),
  history: (id: number) =>
    request<EntryHistory>("GET", `/product-entries/${id}/history`),
  stats: (id: number) =>
    request<EntryStats>("GET", `/product-entries/${id}/stats`),

  listGroups: () => request<Group[]>("GET", "/groups"),
  getGroup: (slug: string) => request<Group>("GET", `/groups/${slug}`),
  createGroup: (payload: { name: string; brand?: string }) =>
    request<Group>("POST", "/groups", payload),
  deleteGroup: (slug: string) => request<void>("DELETE", `/groups/${slug}`),
  compare: (slug: string, staleHours?: number) =>
    request<Comparison>(
      "GET",
      `/groups/${slug}/compare${staleHours ? `?stale_hours=${staleHours}` : ""}`,
    ),
};
