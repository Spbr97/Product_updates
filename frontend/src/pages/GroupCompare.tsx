import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Comparison } from "../api";
import { describeCell, isBest } from "../lib/cellState";
import { priceText } from "../lib/listingState";

/**
 * The grid: every model down the side, every shop across the top.
 *
 * Each cell says which kind of answer it is. A shop that refused us reads "Refused", not
 * a blank -- a blank in a row of prices reads as "nothing to see here", which is the one
 * thing it does not mean.
 */
export function GroupCompare() {
  const { slug = "" } = useParams();
  const [grid, setGrid] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setGrid(null);
    setError(null);
    api
      .compare(slug)
      .then((data) => live && setGrid(data))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [slug]);

  if (error) {
    return (
      <>
        <div className="errors" role="alert">
          {error}
        </div>
        <p>
          <Link to="/compare">Back to comparisons</Link>
        </p>
      </>
    );
  }

  if (grid === null) return <p className="lede">Loading…</p>;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{grid.group_name}</h1>
          <p className="lede">
            {grid.brand ? `${grid.brand} · ` : ""}
            {grid.rows.length} model{grid.rows.length === 1 ? "" : "s"} ·{" "}
            {grid.stores.length} shop{grid.stores.length === 1 ? "" : "s"}
          </p>
        </div>
        <div className="actions">
          <Link className="button ghost" to="/compare">
            All comparisons
          </Link>
        </div>
      </div>

      {grid.mixed_currency && (
        <div className="notice warn" role="note">
          These shops quote different currencies ({grid.currencies.join(", ")}). Prices are
          never converted, so no single cheapest shop is offered — comparing them would
          mean inventing an exchange rate.
        </div>
      )}

      {grid.rows.length === 0 && (
        <div className="card empty">
          <p>No shop carries a model in this group yet.</p>
          <p className="hint">
            Attach a tracked listing with{" "}
            <code>product-tracker groups attach &lt;PRODUCT-ID&gt; {slug}</code>.
          </p>
        </div>
      )}

      {grid.rows.length > 0 && (
        <div className="grid-scroll">
          <table className="listing compare">
            <thead>
              <tr>
                <th scope="col">Model</th>
                {grid.stores.map((store) => (
                  <th key={store.slug} scope="col">
                    {store.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {grid.rows.map((row) => (
                <tr key={row.variant_id ?? row.label}>
                  <th scope="row">
                    {row.label}
                    {Object.keys(row.attributes).length > 0 && (
                      <span className="specs">
                        {Object.values(row.attributes).join(" · ")}
                      </span>
                    )}
                  </th>
                  {grid.stores.map((store) => {
                    const cell = row.cells[store.slug];
                    if (!cell) {
                      return (
                        <td key={store.slug}>
                          <span className="cell-state muted">—</span>
                        </td>
                      );
                    }
                    const view = describeCell(cell);
                    const best = !grid.mixed_currency && isBest(row, store.slug);
                    return (
                      <td key={store.slug} className={best ? "best" : undefined}>
                        {view.comparable ? (
                          <span className="cell-price" title={view.explanation}>
                            {priceText(cell.price, cell.currency)}
                            {best && <span className="badge good">cheapest</span>}
                          </span>
                        ) : (
                          <span
                            className={`cell-state ${view.tone}`}
                            title={view.explanation}
                          >
                            {view.label}
                          </span>
                        )}
                        {cell.is_stale && view.comparable && (
                          <span className="cell-state muted">not checked recently</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="hint">
        A cell that is not a price says which kind of silence it is. Hover it for the
        detail. &ldquo;Refused&rdquo; and &ldquo;No price&rdquo; are not out of stock.
      </p>
    </>
  );
}
