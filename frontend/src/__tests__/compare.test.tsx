import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { renderApp } from "../test/render";
import { reset, seedGroup } from "../test/server";
import type { Comparison, ComparisonCell, Group } from "../api";

/**
 * The comparison grid in the browser.
 *
 * The grid is where the project's central rule is easiest to break: four different
 * situations produce a cell with no price, and drawing them all as an empty square would
 * repeat in CSS the exact mistake the data model refuses to make. So most of these tests
 * are about what a *non-price* cell says.
 */

function cell(over: Partial<ComparisonCell> = {}): ComparisonCell {
  return {
    status: "ok",
    price: "82900.00",
    currency: "INR",
    availability: "in_stock",
    product_id: 1,
    url: "https://shop.example.com/p/1",
    last_checked_at: "2026-09-03T10:00:00Z",
    is_stale: false,
    ...over,
  };
}

const GROUP: Group = {
  id: 1,
  slug: "galaxy-s25",
  name: "Galaxy S25",
  brand: "Samsung",
  notes: null,
  variants: [],
  created_at: "2026-09-01T00:00:00Z",
};

function grid(over: Partial<Comparison> = {}): Comparison {
  return {
    group_slug: "galaxy-s25",
    group_name: "Galaxy S25",
    brand: "Samsung",
    stores: [
      { slug: "flipkart", name: "Flipkart" },
      { slug: "croma", name: "Croma" },
      { slug: "bigbasket", name: "BigBasket" },
    ],
    rows: [
      {
        variant_id: 1,
        label: "256GB / Navy",
        attributes: { storage: "256GB", colour: "Navy" },
        cells: {
          flipkart: cell({ price: "79999.00" }),
          croma: cell({ status: "blocked", price: null, availability: "unknown" }),
          bigbasket: cell({ status: "no_price", price: null, availability: "unknown" }),
        },
        best_price: "79999.00",
        best_stores: ["flipkart"],
        spread: null,
      },
    ],
    generated_at: "2026-09-03T10:00:00Z",
    currencies: ["INR"],
    mixed_currency: false,
    ...over,
  };
}

beforeEach(() => {
  reset();
  seedGroup(GROUP, grid());
});

describe("the comparison grid", () => {
  it("lists groups and links into the grid", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/compare");

    await user.click(await screen.findByRole("link", { name: "Galaxy S25" }));

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/compare/galaxy-s25"),
    );
  });

  it("draws a shop per column and a model per row", async () => {
    renderApp("/compare/galaxy-s25");

    await screen.findByRole("columnheader", { name: "Flipkart" });
    expect(screen.getByRole("columnheader", { name: "Croma" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: /256GB \/ Navy/ })).toBeInTheDocument();
  });

  it("a shop that refused us says so, and never 'out of stock'", async () => {
    renderApp("/compare/galaxy-s25");
    await screen.findByRole("columnheader", { name: "Croma" });

    const refused = screen.getByText("Refused");
    expect(refused).toHaveAttribute("title", expect.stringContaining("told us nothing"));

    // Scoped to the grid itself: the page's explanatory copy mentions the phrase on
    // purpose, and asserting over the whole document would match that instead.
    const row = screen.getByRole("row", { name: /256GB \/ Navy/ });
    expect(within(row).queryByText(/out of stock/i)).toBeNull();
  });

  it("a readable page with no price is distinct from a refusal", async () => {
    renderApp("/compare/galaxy-s25");
    await screen.findByRole("columnheader", { name: "BigBasket" });

    // Two different silences, two different words. Collapsing them is the bug.
    expect(screen.getByText("No price")).toBeInTheDocument();
    expect(screen.getByText("Refused")).toBeInTheDocument();
  });

  it("marks the cheapest shop", async () => {
    renderApp("/compare/galaxy-s25");
    await screen.findByRole("columnheader", { name: "Flipkart" });

    expect(screen.getByText("cheapest")).toBeInTheDocument();
  });

  it("offers no cheapest when the shops quote different currencies", async () => {
    reset();
    seedGroup(
      GROUP,
      grid({
        mixed_currency: true,
        currencies: ["INR", "USD"],
        rows: [
          {
            variant_id: 1,
            label: "256GB / Navy",
            attributes: {},
            cells: {
              flipkart: cell({ price: "79999.00" }),
              croma: cell({ price: "899.00", currency: "USD" }),
              bigbasket: cell({ status: "not_tracked", price: null }),
            },
            best_price: null,
            best_stores: [],
            spread: null,
          },
        ],
      }),
    );
    renderApp("/compare/galaxy-s25");

    expect(await screen.findByRole("note")).toHaveTextContent(/never converted/i);
    expect(screen.queryByText("cheapest")).toBeNull();
  });

  it("an unknown group explains itself instead of rendering an empty grid", async () => {
    renderApp("/compare/does-not-exist");

    expect(await screen.findByRole("alert")).toHaveTextContent(/no product group/i);
  });

  it("a group nothing is attached to says so", async () => {
    reset();
    seedGroup(GROUP, grid({ rows: [], stores: [] }));
    renderApp("/compare/galaxy-s25");

    expect(await screen.findByText(/No shop carries a model/i)).toBeInTheDocument();
  });

  it("creating a group adds it to the list", async () => {
    const user = userEvent.setup();
    reset();
    renderApp("/compare");
    await screen.findByLabelText("Name");

    await user.type(screen.getByLabelText("Name"), "Pixel 10");
    await user.click(screen.getByRole("button", { name: /create group/i }));

    expect(await screen.findByRole("link", { name: "Pixel 10" })).toBeInTheDocument();
  });

  it("shows the specs beside the model when the group has them", async () => {
    renderApp("/compare/galaxy-s25");

    const row = await screen.findByRole("rowheader", { name: /256GB \/ Navy/ });
    expect(within(row).getByText(/256GB · Navy/)).toBeInTheDocument();
  });
});
