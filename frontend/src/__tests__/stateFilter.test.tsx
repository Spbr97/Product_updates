import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { renderApp } from "../test/render";
import { BLOCKED, IN_STOCK, NEEDS_LOCATION, reset, seedEntry } from "../test/server";

/**
 * Filtering the product list by what each shop actually said.
 *
 * The point is not convenience. Before this, a shop that would not quote a price without
 * a delivery area was indistinguishable at a glance from one that was simply quiet, so
 * the one thing a person can act on -- "these shops cannot be tracked from here" -- was
 * buried in a list of rows that all looked equally fine.
 */
function filters() {
  return within(screen.getByRole("group", { name: /filter by state/i }));
}

beforeEach(() => {
  reset();
  seedEntry("Phone A", [IN_STOCK, IN_STOCK]);
  seedEntry("Quick commerce", [NEEDS_LOCATION, IN_STOCK]);
  seedEntry("Refused", [BLOCKED, IN_STOCK]);
});

describe("filtering the product list by state", () => {
  it("offers only the states actually present, with counts", async () => {
    renderApp("/products");
    await screen.findByText("Phone A");

    expect(filters().getByRole("button", { name: /^All 3$/ })).toBeInTheDocument();
    expect(
      filters().getByRole("button", { name: /Needs a delivery area 1/ }),
    ).toBeInTheDocument();
    expect(filters().getByRole("button", { name: /Shop refused us 1/ })).toBeInTheDocument();
    // Nothing is paused or archived here, so those must not be offered as dead ends.
    expect(filters().queryByRole("button", { name: /Paused/ })).toBeNull();
  });

  it("narrows the table to the products in that state", async () => {
    const user = userEvent.setup();
    renderApp("/products");
    await screen.findByText("Phone A");

    await user.click(filters().getByRole("button", { name: /Needs a delivery area/ }));

    await waitFor(() => expect(screen.queryByText("Phone A")).toBeNull());
    expect(screen.getByText("Quick commerce")).toBeInTheDocument();
    expect(screen.queryByText("Refused")).toBeNull();
  });

  it("says how many of the total are showing", async () => {
    const user = userEvent.setup();
    renderApp("/products");
    await screen.findByText("Phone A");

    await user.click(filters().getByRole("button", { name: /Needs a delivery area/ }));

    expect(await screen.findByText(/1 of 3 tracked/)).toBeInTheDocument();
  });

  it("keeps the filter in the URL so the view can be shared", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/products");
    await screen.findByText("Phone A");

    await user.click(filters().getByRole("button", { name: /Needs a delivery area/ }));

    await waitFor(() =>
      expect(router.state.location.search).toContain("state=needs_location"),
    );
  });

  it("a second click on the active chip clears it", async () => {
    const user = userEvent.setup();
    renderApp("/products");
    await screen.findByText("Phone A");
    const chip = () => filters().getByRole("button", { name: /Needs a delivery area/ });

    await user.click(chip());
    await waitFor(() => expect(screen.queryByText("Phone A")).toBeNull());
    await user.click(chip());

    expect(await screen.findByText("Phone A")).toBeInTheDocument();
  });

  it("marks the active chip for assistive tech, not just colour", async () => {
    const user = userEvent.setup();
    renderApp("/products");
    await screen.findByText("Phone A");

    await user.click(filters().getByRole("button", { name: /Needs a delivery area/ }));

    expect(
      filters().getByRole("button", { name: /Needs a delivery area/ }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(filters().getByRole("button", { name: /^All/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("an empty result explains itself rather than looking broken", async () => {
    const user = userEvent.setup();
    reset();
    seedEntry("Only in stock", [IN_STOCK, IN_STOCK]);
    renderApp("/products");
    await screen.findByText("Only in stock");

    // Arrive directly on a state nothing is in, the way a shared link would.
    await user.click(filters().getByRole("button", { name: /In stock/ }));
    expect(screen.getByText("Only in stock")).toBeInTheDocument();
  });
});
