import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderApp } from "../test/render";
import { db, reset } from "../test/server";

const AMZ = "https://www.amazon.in/dp/B0EDIT01";
const AMZ2 = "https://www.amazon.in/dp/B0EDIT02";
const FLK = "https://www.flipkart.com/x/p/itmedit01";

function seed() {
  reset();
  const now = new Date().toISOString();
  db.entries.set(1, {
    id: 1,
    product_name: "Original name",
    status: "active",
    created_at: now,
    updated_at: now,
    deleted_at: null,
    listings: [
      {
        id: 2,
        store: "amazon-in",
        store_name: "Amazon India",
        product_name: "amz",
        url: AMZ,
        product_id: 20,
        price: "61470.00",
        currency: "INR",
        availability: "in_stock",
        tracking_status: "active",
        last_checked_at: now,
        last_check_status: "success",
        last_check_error: null,
        is_active: true,
        deactivated_at: null,
      },
      {
        id: 3,
        store: "flipkart",
        store_name: "Flipkart",
        product_name: "flk",
        url: FLK,
        product_id: 30,
        price: "79999.00",
        currency: "INR",
        availability: "in_stock",
        tracking_status: "active",
        last_checked_at: now,
        last_check_status: "success",
        last_check_error: null,
        is_active: true,
        deactivated_at: null,
      },
    ],
  });
  db.nextId = 2;
}

describe("Editing a Product Entry (SDD §58.10, §58.12)", () => {
  beforeEach(seed);

  it("prefills the form with the current values", async () => {
    renderApp("/products/1/edit");
    await waitFor(() =>
      expect(screen.getByLabelText("Product name")).toHaveValue("Original name"),
    );
    expect(screen.getByLabelText("Amazon India URL")).toHaveValue(AMZ);
  });

  it("a rename updates the same entry id and returns to its page", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/products/1/edit");
    const nameInput = await screen.findByLabelText("Product name");
    await user.clear(nameInput);
    await user.type(nameInput, "Renamed S25");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/products/1"));
    expect(await screen.findByText("Renamed S25")).toBeInTheDocument();
    expect(db.entries.get(1)!.id).toBe(1);
  });

  it("changing a URL re-points that listing and the entry keeps its history", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/products/1/edit");
    const urlInput = await screen.findByLabelText("Amazon India URL");
    await user.clear(urlInput);
    await user.type(urlInput, AMZ2);
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/products/1"));
    // Same entry, the Amazon listing now points at the new URL, and the recorded
    // observation is still on the detail page.
    expect(db.entries.get(1)!.listings[0].url).toBe(AMZ2);
    expect(await screen.findByRole("heading", { name: "Recent prices" })).toBeInTheDocument();
  });

  it("names the retailer when a URL is changed to the wrong shop", async () => {
    const user = userEvent.setup();
    renderApp("/products/1/edit");
    const urlInput = await screen.findByLabelText("Amazon India URL");
    await user.clear(urlInput);
    await user.type(urlInput, FLK); // a Flipkart URL in the Amazon listing
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByText(/must stay a Amazon India link/i),
    ).toBeInTheDocument();
  });

  it("a refresh of the detail page never duplicates the entry", async () => {
    const spy = vi.fn();
    renderApp("/products/1");
    await screen.findByRole("heading", { name: "Original name" });
    // Re-mount three times; the store must still hold exactly one entry.
    for (let i = 0; i < 3; i++) {
      renderApp("/products/1");
      await screen.findAllByRole("heading", { name: "Original name" });
    }
    spy();
    expect(db.entries.size).toBe(1);
  });
});
