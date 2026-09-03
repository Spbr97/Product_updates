// What a comparison cell is allowed to say.
//
// The same rule as listingState.ts, applied to the grid: four different situations
// produce a cell with no price, and only one of them is a fact about the product. A grid
// that draws them all as an empty square is the tracker's central mistake made in CSS
// instead of in the data model -- and it is worse here, because a blank cell in a row of
// prices reads as "nothing to see", which is precisely wrong for a shop that blocked us.

import type { CellStatus, ComparisonCell } from "../api";
import type { Tone } from "./listingState";

export interface CellView {
  label: string;
  tone: Tone;
  /** Long form, for a title attribute. Says what we do and do not know. */
  explanation: string;
  /** True when the cell has a price worth comparing against other shops. */
  comparable: boolean;
}

const VIEWS: Record<CellStatus, CellView> = {
  ok: {
    label: "",
    tone: "good",
    explanation: "Read cleanly from the shop.",
    comparable: true,
  },
  out_of_stock: {
    label: "Out of stock",
    tone: "bad",
    explanation: "The shop says this is not buyable. Its own statement, not a guess.",
    comparable: false,
  },
  no_price: {
    label: "No price",
    tone: "caution",
    explanation:
      "The page was read but carried no price. Some shops will not quote one without a delivery area. This says nothing about stock.",
    comparable: false,
  },
  blocked: {
    label: "Refused",
    tone: "caution",
    explanation:
      "The shop declined the request. It has told us nothing about the price or the stock.",
    comparable: false,
  },
  failed: {
    label: "Check failed",
    tone: "bad",
    explanation:
      "The last check did not complete. The product may be perfectly fine; we could not read it.",
    comparable: false,
  },
  never_checked: {
    label: "Not checked",
    tone: "muted",
    explanation: "Tracked here, but no successful check yet.",
    comparable: false,
  },
  not_tracked: {
    label: "—",
    tone: "muted",
    explanation: "No link has been added for this model at this shop.",
    comparable: false,
  },
};

export function describeCell(cell: ComparisonCell): CellView {
  return VIEWS[cell.status] ?? VIEWS.not_tracked;
}

/**
 * Whether this cell is one of the cheapest in its row.
 *
 * Driven by the API's own `best_stores`, not recomputed here: the server already knows
 * about mixed currencies and ties, and a second implementation would eventually disagree
 * with the first.
 */
export function isBest(row: { best_stores: string[] }, storeSlug: string): boolean {
  return row.best_stores.includes(storeSlug);
}
