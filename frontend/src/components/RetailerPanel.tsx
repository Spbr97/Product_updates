import { useState } from "react";
import { api, type ListingResponse } from "../api";
import { describe, priceText } from "../lib/listingState";

/**
 * One shop's panel. "Check now" refreshes only this panel's data, so checking Amazon can
 * never make Flipkart's column appear to change on its own.
 */
export function RetailerPanel({
  entryId,
  listing,
  isCheapest,
  onChanged,
}: {
  entryId: number;
  listing: ListingResponse;
  isCheapest: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const view = describe(listing);
  const checked = listing.last_checked_at
    ? new Date(listing.last_checked_at).toLocaleString()
    : "never";

  async function check() {
    if (busy) return;
    setBusy(true);
    try {
      await api.checkListing(entryId, listing.id);
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={`panel${listing.is_active ? "" : " inactive"}`}>
      <header>
        <h3>{listing.store_name}</h3>
        {isCheapest && <span className="badge good">Cheapest</span>}
      </header>

      <p className="your-name">{listing.product_name}</p>
      <p className="price">{priceText(listing.price, listing.currency)}</p>

      <p className={`state ${view.tone}`}>{view.label}</p>
      <p className="explain">{view.explanation}</p>

      <dl className="meta">
        <dt>Last checked</dt>
        <dd>{checked}</dd>
        <dt>Check status</dt>
        <dd>{listing.last_check_status ?? "—"}</dd>
      </dl>

      <div className="panel-actions">
        <a
          className="button ghost small"
          href={listing.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open shop
        </a>
        {listing.is_active && (
          <button className="button small" onClick={check} disabled={busy}>
            {busy ? "checking…" : "Check this shop"}
          </button>
        )}
      </div>
    </article>
  );
}
