import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type ProductEntry, type ProductEntryStatus } from "../api";
import { describe, priceText } from "../lib/listingState";

const COLUMNS = ["amazon-in", "flipkart"] as const;
const COLUMN_NAMES: Record<string, string> = {
  "amazon-in": "Amazon India",
  flipkart: "Flipkart",
};

/** Everything the account tracks, one row per entry, a stable column per shop. */
export function ProductList() {
  const [params, setParams] = useSearchParams();
  const status = (params.get("status") as ProductEntryStatus) ?? "active";
  const [entries, setEntries] = useState<ProductEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setEntries(null);
    setError(null);
    api
      .listEntries(status)
      .then((page) => live && setEntries(page.items))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [status]);

  const archived = status === "archived";

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Products</h1>
          <p className="lede">
            {entries === null ? "…" : `${entries.length} tracked`}
            {archived ? " (archived)" : ""}.
          </p>
        </div>
        <div className="actions">
          <button
            className="button ghost"
            onClick={() =>
              setParams(archived ? {} : { status: "archived" })
            }
          >
            {archived ? "Show active" : "Show archived"}
          </button>
          <Link className="button primary" to="/products/new">
            Add product
          </Link>
        </div>
      </div>

      {error && (
        <div className="errors" role="alert">
          {error}
        </div>
      )}

      {entries !== null && entries.length === 0 && (
        <div className="card empty">
          <p>Nothing here yet.</p>
          <p className="hint">
            Add a product with its Amazon and Flipkart links, and both shops are tracked
            separately from then on.
          </p>
          <Link className="button primary" to="/products/new">
            Add your first product
          </Link>
        </div>
      )}

      {entries !== null && entries.length > 0 && (
        <table className="listing">
          <thead>
            <tr>
              <th scope="col">Product</th>
              {COLUMNS.map((c) => (
                <th key={c} scope="col">
                  {COLUMN_NAMES[c]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => {
              const byStore = new Map(
                entry.listings.filter((l) => l.is_active).map((l) => [l.store, l]),
              );
              return (
                <tr key={entry.id}>
                  <th scope="row">
                    <Link to={`/products/${entry.id}`}>{entry.product_name}</Link>
                  </th>
                  {COLUMNS.map((c) => {
                    const l = byStore.get(c);
                    if (!l) {
                      return (
                        <td key={c}>
                          <span className="cell-state muted">not tracked</span>
                        </td>
                      );
                    }
                    const view = describe(l);
                    return (
                      <td key={c}>
                        <span className="cell-price">
                          {priceText(l.price, l.currency)}
                        </span>
                        <span className={`cell-state ${view.tone}`}>{view.label}</span>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}
