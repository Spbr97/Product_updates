import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { renderApp } from "../test/render";
import { db, reset, server } from "../test/server";
import type { ProductEntry } from "../api";

function seedOne(): ProductEntry {
  reset();
  const now = new Date().toISOString();
  const entry: ProductEntry = {
    id: 1,
    product_name: "Samsung Galaxy S25 256GB",
    status: "active",
    created_at: now,
    updated_at: now,
    deleted_at: null,
    listings: [
      {
        id: 2,
        store: "amazon-in",
        store_name: "Amazon India",
        product_name: "S25 on Amazon",
        url: "https://www.amazon.in/dp/B0TEST01",
        product_id: 20,
        price: null,
        currency: null,
        availability: "unknown",
        tracking_status: "active",
        last_checked_at: null,
        last_check_status: null,
        last_check_error: null,
        is_active: true,
        deactivated_at: null,
      },
      {
        id: 3,
        store: "flipkart",
        store_name: "Flipkart",
        product_name: "S25 on Flipkart",
        url: "https://www.flipkart.com/x/p/itmtest01",
        product_id: 30,
        price: null,
        currency: null,
        availability: "unknown",
        tracking_status: "active",
        last_checked_at: null,
        last_check_status: null,
        last_check_error: null,
        is_active: true,
        deactivated_at: null,
      },
    ],
  };
  db.entries.set(1, entry);
  db.nextId = 2;
  return entry;
}

describe("Product Entry detail (SDD §51-52, §58.7-13)", () => {
  beforeEach(seedOne);

  it("shows both retailers independently, with separate prices", async () => {
    renderApp("/products/1");
    await screen.findByRole("heading", { name: "Samsung Galaxy S25 256GB" });

    expect(screen.getByText("S25 on Amazon")).toBeInTheDocument(); // the user's name, panel-only
    expect(screen.getByText("S25 on Flipkart")).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "Check now" }));
    await waitFor(() => {
      // Each price shows in its panel and again in the comparison row.
      expect(screen.getAllByText("₹61,470").length).toBeGreaterThan(0);
      expect(screen.getAllByText("₹79,999").length).toBeGreaterThan(0);
    });
  });

  it("a fresh entry says 'Not checked yet' and never 'Out of stock'", async () => {
    renderApp("/products/1");
    await screen.findByRole("heading", { name: "Samsung Galaxy S25 256GB" });
    expect(screen.getAllByText("Not checked yet").length).toBeGreaterThan(0);
    expect(screen.queryByText("Out of stock")).not.toBeInTheDocument();
  });

  it("a shop that refused us is reported as that, not as sold out", async () => {
    seedOne();
    // Amazon's own check comes back blocked; Flipkart succeeds.
    server.use(
      http.post("/api/v1/product-entries/1/check", () => {
        const e = db.entries.get(1)!;
        const amazon = e.listings[0];
        amazon.last_checked_at = new Date().toISOString();
        amazon.last_check_status = "failed";
        amazon.last_check_error = "blocked";
        const flk = e.listings[1];
        flk.price = "79999.00";
        flk.currency = "INR";
        flk.availability = "in_stock";
        flk.last_check_status = "success";
        flk.last_checked_at = new Date().toISOString();
        return HttpResponse.json({ product_entry_id: 1, results: [] });
      }),
    );
    renderApp("/products/1");
    await screen.findByRole("heading", { name: "Samsung Galaxy S25 256GB" });
    await userEvent.setup().click(screen.getByRole("button", { name: "Check now" }));

    await waitFor(() =>
      expect(screen.getAllByText("Shop refused us").length).toBeGreaterThan(0),
    );
    expect(screen.queryByText("Out of stock")).not.toBeInTheDocument();
    // Flipkart's price is still on the page, beside Amazon's failure.
    expect(screen.getAllByText("₹79,999").length).toBeGreaterThan(0);
  });

  it("has the four detail sections: comparison, history, statistics, alerts", async () => {
    renderApp("/products/1");
    await screen.findByRole("heading", { name: "Samsung Galaxy S25 256GB" });
    for (const name of ["Price comparison", "Recent prices", "Statistics", "Alerts"]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
  });

  it("pause then resume reflects on the page", async () => {
    const user = userEvent.setup();
    renderApp("/products/1");
    await screen.findByRole("heading", { name: "Samsung Galaxy S25 256GB" });

    await user.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() =>
      expect(screen.getAllByText("Paused").length).toBeGreaterThan(0),
    );
    await user.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() =>
      expect(screen.queryByText("Paused")).not.toBeInTheDocument(),
    );
  });

  it("archiving takes it out of the active list", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { router } = renderApp("/products/1");
    await screen.findByRole("heading", { name: "Samsung Galaxy S25 256GB" });

    await user.click(screen.getByRole("button", { name: "Archive product" }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/products"),
    );
    await waitFor(() =>
      expect(
        screen.queryByText("Samsung Galaxy S25 256GB"),
      ).not.toBeInTheDocument(),
    );
  });

  it("an unknown entry shows a not-found card, not a crash", async () => {
    renderApp("/products/999");
    expect(await screen.findByText("No such product.")).toBeInTheDocument();
  });

  it("checking one shop does not re-read the other", async () => {
    const user = userEvent.setup();
    let entryChecks = 0;
    let listingChecks = 0;
    server.use(
      http.post("/api/v1/product-entries/1/check", () => {
        entryChecks += 1;
        return HttpResponse.json({ product_entry_id: 1, results: [] });
      }),
      http.post("/api/v1/product-entries/1/listings/:lid/check", ({ params }) => {
        listingChecks += 1;
        const e = db.entries.get(1)!;
        const l = e.listings.find((x) => x.id === Number(params.lid))!;
        l.price = "61470.00";
        l.currency = "INR";
        l.availability = "in_stock";
        l.last_check_status = "success";
        l.last_checked_at = new Date().toISOString();
        return HttpResponse.json({ product_entry_id: 1, results: [] });
      }),
    );
    renderApp("/products/1");
    await screen.findByRole("heading", { name: "Samsung Galaxy S25 256GB" });

    const amazonPanel = screen.getByText("S25 on Amazon").closest("article")!;
    await user.click(
      within(amazonPanel).getByRole("button", { name: "Check this shop" }),
    );
    await waitFor(() => expect(listingChecks).toBe(1));
    expect(entryChecks).toBe(0);
  });
});
