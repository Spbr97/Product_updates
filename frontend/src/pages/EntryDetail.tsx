import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  type EntryHistory,
  type EntryStats,
  type ProductEntry,
} from "../api";
import { cheapestListingId, describe, priceText } from "../lib/listingState";
import { RetailerPanel } from "../components/RetailerPanel";

/**
 * The Product Entry detail page (SDD §51/§52): per-retailer panels, a comparison table,
 * per-shop history and statistics, and the alerts already on the product. Everything is
 * per retailer -- two shops' observations are two series, never one interleaved table.
 */
export function EntryDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const entryId = Number(id);
  const [entry, setEntry] = useState<ProductEntry | null>(null);
  const [history, setHistory] = useState<EntryHistory | null>(null);
  const [stats, setStats] = useState<EntryStats | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [pending, setPending] = useState(false);

  const load = useCallback(() => {
    api.getEntry(entryId).then(setEntry).catch(() => setNotFound(true));
    api.history(entryId).then(setHistory).catch(() => setHistory(null));
    api.stats(entryId).then(setStats).catch(() => setStats(null));
  }, [entryId]);

  useEffect(load, [load]);

  if (notFound) {
    return (
      <div className="card empty">
        <p>No such product.</p>
        <Link className="button" to="/products">
          Back to products
        </Link>
      </div>
    );
  }
  if (!entry) return <p className="lede">Loading…</p>;

  const active = entry.listings.filter((l) => l.is_active);
  const cheapest = cheapestListingId(entry.listings);
  const paused =
    active.length > 0 && active.every((l) => l.tracking_status === "paused");
  const priced = active.filter((l) => l.price !== null);
  const mixedCurrency = new Set(priced.map((l) => l.currency)).size > 1;

  async function act(fn: () => Promise<unknown>) {
    if (pending) return;
    setPending(true);
    try {
      await fn();
      load();
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{entry.product_name}</h1>
          <p className="lede">
            Entry #{entry.id}
            {entry.status === "archived" && (
              <>
                {" "}
                &middot; <span className="badge muted">Archived</span>
              </>
            )}
          </p>
        </div>
        <div className="actions">
          <Link className="button ghost" to={`/products/${entry.id}/edit`}>
            Edit
          </Link>
          <button
            className="button"
            disabled={pending}
            onClick={() =>
              act(() =>
                paused ? api.resumeEntry(entry.id) : api.pauseEntry(entry.id),
              )
            }
          >
            {paused ? "Resume" : "Pause"}
          </button>
          <button
            className="button primary"
            disabled={pending}
            onClick={() => act(() => api.checkEntry(entry.id))}
          >
            Check now
          </button>
        </div>
      </div>

      <section className="panels">
        {entry.listings.map((l) => (
          <RetailerPanel
            key={l.id}
            entryId={entry.id}
            listing={l}
            isCheapest={l.id === cheapest}
            onChanged={load}
          />
        ))}
      </section>

      <section>
        <h2>Price comparison</h2>
        {active.length > 1 ? (
          <>
            <table className="compare">
              <thead>
                <tr>
                  <th scope="col"></th>
                  {active.map((l) => (
                    <th key={l.id} scope="col">
                      {l.store_name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">Current price</th>
                  {active.map((l) => (
                    <td key={l.id} className={l.id === cheapest ? "best" : ""}>
                      {priceText(l.price, l.currency)}
                    </td>
                  ))}
                </tr>
                <tr>
                  <th scope="row">Availability</th>
                  {active.map((l) => {
                    const v = describe(l);
                    return (
                      <td key={l.id} className={v.tone}>
                        {v.label}
                      </td>
                    );
                  })}
                </tr>
                <tr>
                  <th scope="row">Last checked</th>
                  {active.map((l) => (
                    <td key={l.id}>
                      {l.last_checked_at
                        ? new Date(l.last_checked_at).toLocaleString()
                        : "never"}
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
            {mixedCurrency && (
              <p className="hint">
                These shops quote different currencies, so no cheapest is marked.
                Converting them would invent a rate we do not have.
              </p>
            )}
          </>
        ) : (
          <p className="hint">
            A comparison needs a price from more than one shop. As soon as two have been
            read, this fills in.
          </p>
        )}
      </section>

      <section>
        <h2>Recent prices</h2>
        {(history?.listings ?? []).map((s) => (
          <div key={s.listing_id}>
            <h3>{s.store_name}</h3>
            {s.prices.length > 0 ? (
              <table className="history">
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Price</th>
                  </tr>
                </thead>
                <tbody>
                  {s.prices.map((p, i) => (
                    <tr key={i}>
                      <td>{new Date(p.observed_at).toLocaleString()}</td>
                      <td>
                        {p.currency} {Number(p.price).toLocaleString("en-IN")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="hint">
                Nothing recorded yet. Only meaningful changes are stored, so a price that
                has not moved appears once.
              </p>
            )}
          </div>
        ))}
        <p className="hint">
          Each shop&rsquo;s history is its own. They are never interleaved &mdash; that
          would produce a price series no single listing ever had.
        </p>
      </section>

      <section>
        <h2>Statistics</h2>
        {(stats?.listings ?? []).map((s) => (
          <div key={s.listing_id}>
            <h3>{s.store_name}</h3>
            {s.observations > 0 ? (
              <dl className="stats">
                <div>
                  <dt>Current</dt>
                  <dd>{priceText(s.current, s.currency)}</dd>
                </div>
                <div>
                  <dt>Lowest</dt>
                  <dd>
                    {priceText(s.lowest, s.currency)}
                    <span className="muted">
                      {" "}
                      ({s.lowest_at ? s.lowest_at.slice(0, 10) : "—"})
                    </span>
                  </dd>
                </div>
                <div>
                  <dt>Highest</dt>
                  <dd>{priceText(s.highest, s.currency)}</dd>
                </div>
                <div>
                  <dt>Average</dt>
                  <dd>{priceText(s.average, s.currency)}</dd>
                </div>
                <div>
                  <dt>Since first seen</dt>
                  <dd>{priceText(s.changed_by, s.currency)}</dd>
                </div>
                <div>
                  <dt>Observations</dt>
                  <dd>{s.observations}</dd>
                </div>
              </dl>
            ) : (
              <p className="hint">
                No prices recorded yet, so there is nothing to summarise.
              </p>
            )}
          </div>
        ))}
      </section>

      <section>
        <h2>Alerts</h2>
        <p className="hint">
          Alerts are set per shop through <code>product-tracker alerts</code> or the{" "}
          <code>/api/v1/alerts</code> endpoint, against a listing&rsquo;s product.
        </p>
      </section>

      <section className="danger">
        <h2>Stop tracking</h2>
        <p className="hint">
          Archiving stops scheduled checks and keeps every observation. Nothing is deleted.
        </p>
        <button
          className="button danger"
          disabled={pending}
          onClick={async () => {
            if (!window.confirm("Archive this product? Its history is kept.")) return;
            await api.archiveEntry(entry.id);
            navigate("/products");
          }}
        >
          Archive product
        </button>
      </section>
    </>
  );
}
