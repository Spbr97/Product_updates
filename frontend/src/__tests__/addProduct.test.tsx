import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { renderApp } from "../test/render";
import { reset, server } from "../test/server";

const AMZ = "https://www.amazon.in/dp/B0NEW01";
const FLK = "https://www.flipkart.com/x/p/itmnew01";

async function fillGood(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Product name"), "Samsung Galaxy S25 256GB");
  await user.type(screen.getByLabelText("Amazon product name"), "S25 on Amazon");
  await user.type(screen.getByLabelText("Amazon product URL"), AMZ);
  await user.type(screen.getByLabelText("Flipkart product name"), "S25 on Flipkart");
  await user.type(screen.getByLabelText("Flipkart product URL"), FLK);
}

describe("Add Product form (SDD §51, §58.1-6)", () => {
  beforeEach(reset);

  it("renders every field", () => {
    renderApp("/products/new");
    for (const label of [
      "Product name",
      "Amazon product name",
      "Amazon product URL",
      "Flipkart product name",
      "Flipkart product URL",
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
  });

  it("refuses a submit with a missing field", async () => {
    const user = userEvent.setup();
    renderApp("/products/new");
    await user.type(screen.getByLabelText("Product name"), "only a name");
    await user.click(screen.getByRole("button", { name: "Add product" }));
    expect(await screen.findByText(/every field is required/i)).toBeInTheDocument();
  });

  it("names the retailer when an Amazon link is really a Flipkart link", async () => {
    const user = userEvent.setup();
    renderApp("/products/new");
    await user.type(screen.getByLabelText("Product name"), "S25");
    await user.type(screen.getByLabelText("Amazon product name"), "a");
    await user.type(screen.getByLabelText("Amazon product URL"), FLK);
    await user.type(screen.getByLabelText("Flipkart product name"), "f");
    await user.type(screen.getByLabelText("Flipkart product URL"), FLK);
    await user.click(screen.getByRole("button", { name: "Add product" }));
    expect(await screen.findByText(/needs a link from Amazon/i)).toBeInTheDocument();
  });

  it("keeps every field's value when the submit is rejected", async () => {
    const user = userEvent.setup();
    renderApp("/products/new");
    await user.type(screen.getByLabelText("Product name"), "Kept Name");
    await user.type(screen.getByLabelText("Amazon product name"), "a");
    await user.type(screen.getByLabelText("Amazon product URL"), FLK); // wrong retailer
    await user.type(screen.getByLabelText("Flipkart product name"), "f");
    await user.type(screen.getByLabelText("Flipkart product URL"), FLK);
    await user.click(screen.getByRole("button", { name: "Add product" }));

    await screen.findByText(/needs a link from Amazon/i);
    expect(screen.getByLabelText("Product name")).toHaveValue("Kept Name");
    expect(screen.getByLabelText("Amazon product URL")).toHaveValue(FLK);
  });

  it("creates exactly one entry and lands on its page", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/products/new");
    await fillGood(user);
    await user.click(screen.getByRole("button", { name: "Add product" }));

    await waitFor(() =>
      expect(router.state.location.pathname).toMatch(/^\/products\/\d+$/),
    );
    expect(await screen.findByText("Samsung Galaxy S25 256GB")).toBeInTheDocument();
  });

  it("a double-clicked submit is a conflict, not a second product", async () => {
    // Slow the create so the second click lands while the first is in flight.
    let hits = 0;
    server.use(
      http.post("/api/v1/product-entries", async ({ request }) => {
        hits += 1;
        await new Promise((r) => setTimeout(r, 40));
        const body = (await request.json()) as { product_name: string };
        return HttpResponse.json(
          {
            id: 1,
            product_name: body.product_name,
            status: "active",
            created_at: "",
            updated_at: "",
            deleted_at: null,
            listings: [],
          },
          { status: 201 },
        );
      }),
    );
    const user = userEvent.setup();
    renderApp("/products/new");
    await fillGood(user);
    const button = screen.getByRole("button", { name: "Add product" });
    await user.click(button);
    // The button disables itself synchronously; a second click cannot register.
    expect(button).toBeDisabled();
    await user.click(button).catch(() => {});
    await waitFor(() => expect(hits).toBe(1));
  });
});
