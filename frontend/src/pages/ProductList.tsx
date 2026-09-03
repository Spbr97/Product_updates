import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type ProductEntry, type ProductEntryStatus } from "../api";
import {
  STATE_ORDER,
  STATE_VIEWS,
  describe,
  priceText,
  type StateKey,
} from "../lib/listingState";

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
  const state = params.get("state") as StateKey | null;

  /**
   * How many entries have at least one shop in each state.
   *
   * Counted over entries rather than listings because the list shows one row per entry:
   * a count of 3 has to mean three rows will remain, or the filter lies about itself.
   */
  const counts = useMemo(() => {
    const tally = new Map<StateKey, number>();
    for (const entry of entries ?? []) {
      const states = new Set(
        entry.listings.map((l) => describe(l).key as StateKey),
      );
      for (const key of states) tally.set(key, (tally.get(key) ?? 0) + 1);
    }
    return tally;
  }, [entries]);

  const shown = useMemo(() => {
    if (entries === null || state === null) return entries;
    return entries.filter((entry) =>
      entry.listings.some((l) => describe(l).key === state),
    );
  }, [entries, state]);

  function setState(next: StateKey | null): void {
    const params_: Record<string, string> = {};
    if (archived) params_.status = "archived";
    if (next) params_.state = next;
    setParams(params_);
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Products</h1>
          <p className="lede">
            {entries === null
              ? "…"
              : state === null
                ? `${entries.length} tracked`
                : `${shown?.length ?? 0} of ${entries.length} tracked`}
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

      {entries !== null && entries.length > 0 && (
        <div className="filters" role="group" aria-label="Filter by state">
          <button
            className={`chip${state === null ? " on" : ""}`}
            aria-pressed={state === null}
            onClick={() => setState(null)}
          >
            All <span className="count">{entries.length}</span>
          </button>
          {STATE_ORDER.filter((key) => counts.has(key)).map((key) => (
            <button
              key={key}
              className={`chip ${STATE_VIEWS[key].tone}${state === key ? " on" : ""}`}
              aria-pressed={state === key}
              onClick={() => setState(state === key ? null : key)}
              title={STATE_VIEWS[key].explanation}
            >
              {STATE_VIEWS[key].label} <span className="count">{counts.get(key)}</span>
            </button>
          ))}
        </div>
      )}

      {shown !== null && entries !== null && entries.length > 0 && shown.length === 0 && (
        <div className="card empty">
          <p>No product is in that state right now.</p>
          <button className="button ghost" onClick={() => setState(null)}>
            Show all {entries.length}
          </button>
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

      {shown !== null && shown.length > 0 && (
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
            {shown.map((entry) => {
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
