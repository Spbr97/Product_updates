import { useState } from "react";
import { ApiError, api } from "../api";

const STORE_NAMES: Record<string, string> = {
  "amazon-in": "Amazon",
  flipkart: "Flipkart",
};
const URL_HINTS: Record<string, string> = {
  "amazon-in": "An amazon.in link, or an amzn.in/amzn.to share link.",
  flipkart: "Must be a flipkart.com link.",
};

/**
 * The panel offered in place of a shop an entry does not have a live listing for yet.
 * Entries can be created with just one retailer (AddProduct.tsx), so this is how the other
 * one gets attached afterward instead of that gap being permanent.
 */
export function AddRetailerPanel({
  entryId,
  store,
  onAdded,
}: {
  entryId: number;
  store: string;
  onAdded: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const storeName = STORE_NAMES[store] ?? store;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (name.trim() === "" || url.trim() === "") {
      setError("Both the name and the URL are required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.addListing(entryId, { store, product_name: name, url });
      onAdded();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That didn't go through.");
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <article className="panel inactive">
        <header>
          <h3>{storeName}</h3>
        </header>
        <p className="explain">Not tracked at {storeName} yet.</p>
        <div className="panel-actions">
          <button className="button small" onClick={() => setOpen(true)}>
            Add {storeName} link
          </button>
        </div>
      </article>
    );
  }

  return (
    <article className="panel inactive">
      <header>
        <h3>{storeName}</h3>
      </header>
      {error && (
        <div className="errors" role="alert">
          {error}
        </div>
      )}
      <form onSubmit={submit}>
        <label htmlFor={`add-${store}-name`}>{storeName} product name</label>
        <input
          id={`add-${store}-name`}
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={200}
        />
        <label htmlFor={`add-${store}-url`}>{storeName} product URL</label>
        <input
          id={`add-${store}-url`}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <p className="hint">{URL_HINTS[store] ?? ""}</p>
        <div className="panel-actions">
          <button
            type="button"
            className="button ghost small"
            onClick={() => setOpen(false)}
            disabled={busy}
          >
            Cancel
          </button>
          <button type="submit" className="button small" disabled={busy}>
            {busy ? "Adding…" : "Add"}
          </button>
        </div>
      </form>
    </article>
  );
}
