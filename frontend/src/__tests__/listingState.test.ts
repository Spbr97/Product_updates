import { describe, expect, it } from "vitest";
import {
  cheapestListingId,
  describe as describeState,
  priceText,
} from "../lib/listingState";
import type { ListingResponse } from "../api";

function listing(over: Partial<ListingResponse> = {}): ListingResponse {
  return {
    id: 1,
    store: "amazon-in",
    store_name: "Amazon India",
    product_name: "x",
    url: "https://www.amazon.in/dp/x",
    product_id: 10,
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

describe("listing state resolver (port of presenters.py)", () => {
  it("a shop that refused us is 'blocked', never out of stock", () => {
    const v = describeState(
      listing({
        last_checked_at: "2026-09-03T00:00:00Z",
        last_check_status: "failed",
        last_check_error: "blocked",
      }),
    );
    expect(v.key).toBe("blocked");
    expect(v.label).not.toMatch(/out of stock/i);
  });

  it("a read with no signal is 'unknown', not out of stock", () => {
    const v = describeState(
      listing({ last_checked_at: "2026-09-03T00:00:00Z", last_check_status: "partial" }),
    );
    expect(v.key).toBe("unknown");
  });

  it("only an actual out_of_stock availability produces 'Out of stock'", () => {
    expect(
      describeState(listing({ availability: "out_of_stock", last_checked_at: "x" })).key,
    ).toBe("out_of_stock");
  });

  it("a paused listing says paused whatever the last check was", () => {
    const v = describeState(
      listing({
        tracking_status: "paused",
        availability: "in_stock",
        last_checked_at: "x",
      }),
    );
    expect(v.key).toBe("paused");
  });

  it("a deactivated listing says removed", () => {
    expect(describeState(listing({ is_active: false })).key).toBe("removed");
  });

  it("a fresh listing says not_checked", () => {
    expect(describeState(listing()).key).toBe("not_checked");
  });
});

describe("priceText", () => {
  it("formats INR with the rupee sign", () => {
    expect(priceText("61470.00", "INR")).toBe("₹61,470");
  });
  it("a null price is a dash, never a zero", () => {
    expect(priceText(null, "INR")).toBe("—");
  });
});

describe("cheapestListingId", () => {
  it("is null with fewer than two priced shops", () => {
    expect(cheapestListingId([listing({ id: 1, price: "10", currency: "INR" })])).toBeNull();
  });
  it("is null across different currencies", () => {
    expect(
      cheapestListingId([
        listing({ id: 1, price: "10", currency: "INR" }),
        listing({ id: 2, price: "5", currency: "USD" }),
      ]),
    ).toBeNull();
  });
  it("picks the lowest when the question is meaningful", () => {
    expect(
      cheapestListingId([
        listing({ id: 1, price: "79999", currency: "INR" }),
        listing({ id: 2, price: "61470", currency: "INR" }),
      ]),
    ).toBe(2);
  });
});
