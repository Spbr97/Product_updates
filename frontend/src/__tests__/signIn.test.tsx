import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { renderApp } from "../test/render";
import { server } from "../test/server";
import { IN_STOCK, reset, seedEntry } from "../test/server";
import { setApiKey } from "../api";

/**
 * Signing in.
 *
 * The bug this covers shipped: with accounts configured, every page rendered "missing or
 * invalid X-API-Key" and the app offered no input anywhere to supply one. The API was
 * correct and the deployment was still unusable from a browser, which is the kind of
 * failure that only shows up when someone actually opens it.
 */
function lockTheApi() {
  server.use(
    http.get("/api/v1/product-entries", () =>
      HttpResponse.json(
        { error: { type: "unauthorized", message: "missing or invalid X-API-Key" } },
        { status: 401 },
      ),
    ),
  );
}

beforeEach(() => {
  reset();
  setApiKey(null);
  seedEntry("Galaxy S25", [IN_STOCK, IN_STOCK]);
});

describe("signing in", () => {
  it("a 401 asks for a key instead of dead-ending", async () => {
    lockTheApi();
    renderApp("/products");

    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
  });

  it("says where a key comes from, since the browser cannot make one", async () => {
    lockTheApi();
    renderApp("/products");
    await screen.findByLabelText("API key");

    // An error that does not name the next command is only half an error message.
    expect(screen.getByText(/product-tracker users add/)).toBeInTheDocument();
  });

  it("the key is masked while typing", async () => {
    lockTheApi();
    renderApp("/products");

    expect(await screen.findByLabelText("API key")).toHaveAttribute("type", "password");
  });

  it("submitting stores the key and loads the page", async () => {
    const user = userEvent.setup();
    lockTheApi();
    renderApp("/products");
    await user.type(await screen.findByLabelText("API key"), "pt_a-real-key");

    // The handler is dropped, so the retry after sign-in succeeds like a real one would.
    server.resetHandlers();
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText("Galaxy S25")).toBeInTheDocument();
    expect(window.localStorage.getItem("pt_key")).toBe("pt_a-real-key");
  });

  it("an empty key cannot be submitted", async () => {
    lockTheApi();
    renderApp("/products");
    await screen.findByLabelText("API key");

    expect(screen.getByRole("button", { name: /sign in/i })).toBeDisabled();
  });

  it("signing out clears the key and asks again", async () => {
    const user = userEvent.setup();
    setApiKey("pt_existing");
    renderApp("/products");
    await screen.findByText("Galaxy S25");

    await user.click(screen.getByRole("button", { name: /sign out/i }));

    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(window.localStorage.getItem("pt_key")).toBeNull();
  });

  it("no sign-out button when no key is held", async () => {
    renderApp("/products");
    await screen.findByText("Galaxy S25");

    // An open install (no accounts configured) must not show a control that does nothing.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /sign out/i })).toBeNull(),
    );
  });
});
