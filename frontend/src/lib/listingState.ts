// Turning a listing's state into something the page can say without lying.
//
// A port of src/product_tracker/web/presenters.py. The whole reason it exists is one
// failure mode: a retailer that refused us, a page whose price would not parse, and a
// product that is genuinely sold out are three different facts, and a UI that renders all
// of them as "Out of stock" turns a tracker into a rumour mill.
//
// So every listing resolves to exactly one of the states below, each with its own wording
// and its own tone, and "we could not tell" is one of them rather than a gap filled with
// the nearest confident-sounding alternative.

import type { ListingResponse } from "../api";

export type Tone = "good" | "caution" | "bad" | "muted";

export interface StateView {
  key: string;
  label: string;
  tone: Tone;
  explanation: string;
}

const VIEWS = {
  not_checked: {
    key: "not_checked",
    label: "Not checked yet",
    tone: "muted",
    explanation:
      "Added, but no check has run. The first price arrives with the next scheduled pass.",
  },
  in_stock: {
    key: "in_stock",
    label: "In stock",
    tone: "good",
    explanation: "The shop says this is available.",
  },
  out_of_stock: {
    key: "out_of_stock",
    label: "Out of stock",
    tone: "bad",
    explanation:
      "The shop says this is unavailable. This is the shop's own statement, not a guess.",
  },
  unknown: {
    key: "unknown",
    label: "Stock unknown",
    tone: "caution",
    explanation:
      "The page loaded but published nothing reliable about availability. Unknown is the honest answer; it does not mean out of stock.",
  },
  needs_location: {
    key: "needs_location",
    label: "Needs a delivery area",
    tone: "caution",
    explanation:
      "This shop prices and stocks per delivery area, and we could not set one -- doing so needs a browser session we do not keep. Whatever the page said about stock was about our missing address, not about this product.",
  },
  blocked: {
    key: "blocked",
    label: "Shop refused us",
    tone: "caution",
    explanation:
      "The retailer declined the request. This says nothing about the price or the stock -- only that we were not allowed to look.",
  },
  failed: {
    key: "failed",
    label: "Check failed",
    tone: "bad",
    explanation:
      "The last check did not complete. The product may be perfectly fine; we could not read it.",
  },
  skipped: {
    key: "skipped",
    label: "Check skipped",
    tone: "muted",
    explanation:
      "Nothing was attempted: this shop is being backed off after repeated failures.",
  },
  paused: {
    key: "paused",
    label: "Paused",
    tone: "muted",
    explanation:
      "Scheduled checks are stopped for this product. History is kept.",
  },
  removed: {
    key: "removed",
    label: "Removed",
    tone: "muted",
    explanation:
      "You stopped tracking this shop. Everything already recorded is still here.",
  },
} as const satisfies Record<string, StateView>;

const BLOCKED_ERRORS = new Set(["blocked", "unavailable"]);

/**
 * The one state this listing is in.
 *
 * Order matters. A removed or paused listing is described that way whatever its last check
 * said, because the user's own action is the more relevant fact. After that a successful
 * read wins, then the reason a read failed -- and only a shop that actually said
 * "unavailable" produces "Out of stock".
 */
export function describe(listing: ListingResponse): StateView {
  if (!listing.is_active) return VIEWS.removed;
  if (listing.tracking_status === "paused") return VIEWS.paused;

  if (listing.last_checked_at === null && listing.last_check_status === null) {
    return VIEWS.not_checked;
  }
  if (listing.last_check_status === "skipped") return VIEWS.skipped;

  // A shop's own statement about stock outranks how the check was classified: a listing
  // can be read perfectly and still be sold out. This sits above needs_location on
  // purpose -- where a shop did tell us its stock, saying "needs a delivery area"
  // instead would discard a real finding to explain a missing price.
  if (listing.availability === "out_of_stock") return VIEWS.out_of_stock;
  if (listing.availability === "in_stock") return VIEWS.in_stock;

  // Nothing definite about stock, and the backend knows why: this shop will not answer
  // without a delivery area. Above "unknown" because it is the same honesty with a
  // reason attached, and the reason is the difference between a user retrying forever
  // and a user understanding that this shop cannot be tracked from here.
  if (listing.last_check_error === "needs_location") return VIEWS.needs_location;

  if (listing.last_check_error && BLOCKED_ERRORS.has(listing.last_check_error)) {
    return VIEWS.blocked;
  }
  if (listing.last_check_status === "failed") return VIEWS.failed;

  // Read, but it told us nothing definite. Not a failure, and emphatically not a
  // statement that the product is unavailable.
  return VIEWS.unknown;
}

const SYMBOLS: Record<string, string> = {
  INR: "₹",
  USD: "$",
  GBP: "£",
  EUR: "€",
};

/** The price, or a dash. Never a zero, and never a guess. */
export function priceText(price: string | null, currency: string | null): string {
  if (price === null) return "—";
  const n = Number(price);
  if (!Number.isFinite(n)) return "—";
  const symbol = SYMBOLS[currency ?? ""] ?? (currency ? `${currency} ` : "");
  const body = n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  return `${symbol}${body}`;
}

/**
 * The id of the listing with the lowest price, when that is a meaningful question.
 * `null` when fewer than two shops have a price, or when they are priced in different
 * currencies -- comparing those without a conversion policy would produce a "best deal"
 * that is simply wrong.
 */
export function cheapestListingId(listings: ListingResponse[]): number | null {
  const priced = listings.filter(
    (l) => l.is_active && l.price !== null && Number.isFinite(Number(l.price)),
  );
  if (priced.length < 2) return null;
  if (new Set(priced.map((l) => l.currency)).size > 1) return null;
  let best = priced[0];
  for (const l of priced) {
    if (Number(l.price) < Number(best.price)) best = l;
  }
  return best.id;
}
