/**
 * Signing in by link.
 *
 * Asking somebody to paste a 46-character secret into a password field is asking them to
 * type blind, and a mistyped key is indistinguishable from a revoked one. A link removes
 * that. What it must not do is leave the key sitting in the address bar, where it reaches
 * the browser's history, the next bookmark, and the next screenshot.
 */

import { beforeEach, describe, expect, it } from "vitest";
import { adoptKeyFromUrl, hasApiKey, setApiKey } from "../api";

const KEY = "pt_TESTKEYTESTKEYTESTKEYTESTKEYTESTKEYTESTKEY01";

function visit(href: string): void {
  window.history.replaceState({}, "", href);
}

describe("signing in from a link", () => {
  beforeEach(() => {
    setApiKey(null);
    visit("/ui/");
  });

  it("takes the key out of the query string", () => {
    visit(`/ui/?key=${KEY}`);

    expect(adoptKeyFromUrl()).toBe(true);
    expect(hasApiKey()).toBe(true);
  });

  it("strips the key from the address bar once it is stored", () => {
    visit(`/ui/?key=${KEY}`);

    adoptKeyFromUrl();

    expect(window.location.search).not.toContain(KEY);
    expect(window.location.search).not.toContain("key=");
  });

  it("keeps any other query parameters", () => {
    visit(`/ui/?key=${KEY}&status=archived`);

    adoptKeyFromUrl();

    expect(window.location.search).toContain("status=archived");
    expect(window.location.search).not.toContain("key=");
  });

  it("does nothing to an ordinary visit", () => {
    visit("/ui/");

    expect(adoptKeyFromUrl()).toBe(false);
    expect(hasApiKey()).toBe(false);
  });

  it("ignores an empty key rather than storing one", () => {
    visit("/ui/?key=");

    expect(adoptKeyFromUrl()).toBe(false);
    expect(hasApiKey()).toBe(false);
  });

  it("replaces the history entry instead of adding one", () => {
    /* Otherwise Back walks into a URL still carrying the credential. */
    visit("/ui/");
    const before = window.history.length;
    visit(`/ui/?key=${KEY}`);

    adoptKeyFromUrl();

    expect(window.history.length).toBe(before);
  });
});
