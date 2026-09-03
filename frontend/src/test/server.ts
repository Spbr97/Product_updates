import { setupServer } from "msw/node";
import { HttpResponse, http } from "msw";
import type {
  Comparison,
  EntryHistory,
  EntryStats,
  Group,
  ListingResponse,
  ProductEntry,
} from "../api";

// A tiny in-memory API. It is not the real thing -- the real contract is covered by the
// Python integration suite -- but it is faithful enough for the page tests: it enforces
// the retailer-field domain rule, the duplicate-URL 409, and per-shop isolation, because
// those are the behaviours the UI must render correctly.

const AMZ = "https://www.amazon.in/dp/B0TEST01";
const FLK = "https://www.flipkart.com/x/p/itmtest01";

function listing(
  over: Partial<ListingResponse> & Pick<ListingResponse, "id" | "store" | "store_name" | "url">,
): ListingResponse {
  return {
    product_name: "S25",
    product_id: over.id * 10,
    price: null,
    currency: null,
    availability: "unknown",
    tracking_status: "active",
    last_checked_at: null,
    last_check_status: null,
    last_check_error: null,
    is_active: true,
    deactivated_at: null,
    ...over,
  };
}

interface Store {
  entries: Map<number, ProductEntry>;
  nextId: number;
  groups: Map<string, Group>;
  grids: Map<string, Comparison>;
}

export const db: Store = {
  entries: new Map(),
  nextId: 1,
  groups: new Map(),
  grids: new Map(),
};

export function reset(): void {
  db.entries = new Map();
  db.nextId = 1;
  db.groups = new Map();
  db.grids = new Map();
}

/** Register a group and the grid the compare endpoint should return for it. */
export function seedGroup(group: Group, grid?: Comparison): void {
  db.groups.set(group.slug, group);
  if (grid) db.grids.set(group.slug, grid);
}

function fresh(name: string): ProductEntry {
  const id = db.nextId++;
  const now = new Date().toISOString();
  const entry: ProductEntry = {
    id,
    product_name: name,
    status: "active",
    created_at: now,
    updated_at: now,
    deleted_at: null,
    listings: [
      listing({ id: id * 2, store: "amazon-in", store_name: "Amazon India", url: AMZ }),
      listing({ id: id * 2 + 1, store: "flipkart", store_name: "Flipkart", url: FLK }),
    ],
  };
  db.entries.set(id, entry);
  return entry;
}

function err(status: number, type: string, message: string) {
  return HttpResponse.json({ error: { type, message, detail: null } }, { status });
}

/**
 * Seed an entry whose two shops are in whatever states a test needs.
 *
 * The state resolver reads several fields together, so a test that set only
 * ``availability`` would be asserting against a listing the real API could never
 * produce. This keeps the combinations in one place and honest.
 */
export function seedEntry(
  name: string,
  shops: Partial<ListingResponse>[],
): ProductEntry {
  const entry = fresh(name);
  shops.forEach((over, i) => Object.assign(entry.listings[i], over));
  return entry;
}

/** A listing that was read cleanly and is on sale. */
export const IN_STOCK: Partial<ListingResponse> = {
  price: "69999.00",
  currency: "INR",
  availability: "in_stock",
  last_checked_at: "2026-09-03T10:00:00Z",
  last_check_status: "success",
};

/** A shop that priced per delivery area and would not quote one. */
export const NEEDS_LOCATION: Partial<ListingResponse> = {
  price: null,
  currency: null,
  availability: "unknown",
  last_checked_at: "2026-09-03T10:00:00Z",
  last_check_status: "partial",
  last_check_error: "needs_location",
};

/** A shop that refused the request outright. */
export const BLOCKED: Partial<ListingResponse> = {
  price: null,
  availability: "unknown",
  last_checked_at: "2026-09-03T10:00:00Z",
  last_check_status: "failed",
  last_check_error: "blocked",
};

function storeOf(url: string): string | null {
  if (url.includes("amazon.in")) return "amazon-in";
  if (url.includes("flipkart.com")) return "flipkart";
  return null;
}

export const handlers = [
  http.get("/api/v1/product-entries", ({ request }) => {
    const status = new URL(request.url).searchParams.get("status") ?? "active";
    const items = [...db.entries.values()].filter((e) => e.status === status);
    return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 });
  }),

  http.get("/api/v1/product-entries/:id", ({ params }) => {
    const entry = db.entries.get(Number(params.id));
    return entry ? HttpResponse.json(entry) : err(404, "not_found", "No such entry.");
  }),

  http.post("/api/v1/product-entries", async ({ request }) => {
    const body = (await request.json()) as {
      product_name: string;
      amazon: { product_name: string; url: string };
      flipkart: { product_name: string; url: string };
    };
    if (storeOf(body.amazon.url) !== "amazon-in") {
      return err(422, "invalid_store_url", "the Amazon field needs a link from Amazon");
    }
    if (storeOf(body.flipkart.url) !== "flipkart") {
      return err(422, "invalid_store_url", "the Flipkart field needs a link from Flipkart");
    }
    for (const e of db.entries.values()) {
      for (const l of e.listings) {
        if (l.is_active && (l.url === body.amazon.url || l.url === body.flipkart.url)) {
          return err(
            409,
            "duplicate_listing",
            `this URL is already associated with Product Entry #${e.id}`,
          );
        }
      }
    }
    const entry = fresh(body.product_name);
    entry.listings[0].product_name = body.amazon.product_name;
    entry.listings[0].url = body.amazon.url;
    entry.listings[1].product_name = body.flipkart.product_name;
    entry.listings[1].url = body.flipkart.url;
    return HttpResponse.json(entry, { status: 201 });
  }),

  http.patch("/api/v1/product-entries/:id", async ({ params, request }) => {
    const entry = db.entries.get(Number(params.id));
    if (!entry) return err(404, "not_found", "No such entry.");
    const body = (await request.json()) as { canonical_name: string };
    entry.product_name = body.canonical_name;
    return HttpResponse.json(entry);
  }),

  http.delete("/api/v1/product-entries/:id", ({ params }) => {
    const entry = db.entries.get(Number(params.id));
    if (!entry) return err(404, "not_found", "No such entry.");
    entry.status = "archived";
    entry.deleted_at = new Date().toISOString();
    for (const l of entry.listings) l.is_active = false;
    return new HttpResponse(null, { status: 204 });
  }),

  http.post("/api/v1/product-entries/:id/check", ({ params }) => {
    const entry = db.entries.get(Number(params.id));
    if (!entry) return err(404, "not_found", "No such entry.");
    for (const l of entry.listings.filter((x) => x.is_active)) {
      l.price = l.store === "amazon-in" ? "61470.00" : "79999.00";
      l.currency = "INR";
      l.availability = "in_stock";
      l.last_checked_at = new Date().toISOString();
      l.last_check_status = "success";
    }
    return HttpResponse.json({
      product_entry_id: entry.id,
      results: entry.listings.map((l) => ({
        listing_id: l.id,
        store: l.store,
        status: "success",
        price: l.price,
        currency: l.currency,
        availability: l.availability,
        error_type: null,
        error_detail: null,
      })),
    });
  }),

  http.post(
    "/api/v1/product-entries/:id/listings/:lid/check",
    ({ params }) => {
      const entry = db.entries.get(Number(params.id));
      const l = entry?.listings.find((x) => x.id === Number(params.lid));
      if (!entry || !l) return err(404, "not_found", "No such listing.");
      l.price = l.store === "amazon-in" ? "61470.00" : "79999.00";
      l.currency = "INR";
      l.availability = "in_stock";
      l.last_checked_at = new Date().toISOString();
      l.last_check_status = "success";
      return HttpResponse.json({ product_entry_id: entry.id, results: [] });
    },
  ),

  http.post("/api/v1/product-entries/:id/pause", ({ params }) => {
    const entry = db.entries.get(Number(params.id));
    if (!entry) return err(404, "not_found", "No such entry.");
    for (const l of entry.listings) l.tracking_status = "paused";
    return HttpResponse.json(entry);
  }),

  http.post("/api/v1/product-entries/:id/resume", ({ params }) => {
    const entry = db.entries.get(Number(params.id));
    if (!entry) return err(404, "not_found", "No such entry.");
    for (const l of entry.listings) l.tracking_status = "active";
    return HttpResponse.json(entry);
  }),

  http.patch(
    "/api/v1/product-entries/:id/listings/:lid",
    async ({ params, request }) => {
      const entry = db.entries.get(Number(params.id));
      const l = entry?.listings.find((x) => x.id === Number(params.lid));
      if (!entry || !l) return err(404, "not_found", "No such listing.");
      const body = (await request.json()) as { product_name?: string; url?: string };
      if (body.url !== undefined) {
        if (storeOf(body.url) !== l.store) {
          return err(422, "invalid_store_url", `must stay a ${l.store_name} link`);
        }
        l.url = body.url;
      }
      if (body.product_name !== undefined) l.product_name = body.product_name;
      return HttpResponse.json(l);
    },
  ),

  http.delete(
    "/api/v1/product-entries/:id/listings/:lid",
    ({ params }) => {
      const entry = db.entries.get(Number(params.id));
      const l = entry?.listings.find((x) => x.id === Number(params.lid));
      if (!entry || !l) return err(404, "not_found", "No such listing.");
      l.is_active = false;
      l.deactivated_at = new Date().toISOString();
      return new HttpResponse(null, { status: 204 });
    },
  ),

  http.get("/api/v1/product-entries/:id/history", ({ params }) => {
    const entry = db.entries.get(Number(params.id));
    if (!entry) return err(404, "not_found", "No such entry.");
    const body: EntryHistory = {
      product_entry_id: entry.id,
      listings: entry.listings.map((l) => ({
        listing_id: l.id,
        store: l.store,
        store_name: l.store_name,
        prices:
          l.price === null
            ? []
            : [{ price: l.price, currency: l.currency!, observed_at: l.last_checked_at! }],
        availability:
          l.availability === "unknown"
            ? []
            : [{ availability: l.availability, observed_at: l.last_checked_at! }],
      })),
    };
    return HttpResponse.json(body);
  }),

  http.get("/api/v1/product-entries/:id/stats", ({ params }) => {
    const entry = db.entries.get(Number(params.id));
    if (!entry) return err(404, "not_found", "No such entry.");
    const body: EntryStats = {
      product_entry_id: entry.id,
      listings: entry.listings.map((l) => ({
        listing_id: l.id,
        store: l.store,
        store_name: l.store_name,
        currency: l.currency,
        observations: l.price === null ? 0 : 1,
        current: l.price,
        lowest: l.price,
        highest: l.price,
        average: l.price,
        lowest_at: l.last_checked_at,
        first_observed_at: l.last_checked_at,
        changed_by: null,
        mixed_currency: false,
      })),
    };
    return HttpResponse.json(body);
  }),

  http.get("/api/v1/groups", () => HttpResponse.json([...db.groups.values()])),

  http.post("/api/v1/groups", async ({ request }) => {
    const body = (await request.json()) as { name: string; brand?: string };
    const slug = body.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    if (db.groups.has(slug)) {
      return err(409, "conflict", `a group with slug ${slug} already exists`);
    }
    const group: Group = {
      id: db.groups.size + 1,
      slug,
      name: body.name,
      brand: body.brand ?? null,
      notes: null,
      variants: [],
      created_at: new Date().toISOString(),
    };
    db.groups.set(slug, group);
    return HttpResponse.json(group, { status: 201 });
  }),

  http.get("/api/v1/groups/:slug/compare", ({ params }) => {
    const grid = db.grids.get(String(params.slug));
    return grid
      ? HttpResponse.json(grid)
      : err(404, "not_found", `no product group ${params.slug}`);
  }),
];

export const server = setupServer(...handlers);
