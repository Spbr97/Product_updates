import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api, type ProductEntry } from "../api";

/**
 * Editing never creates a second product, and changing a shop's link keeps everything
 * already recorded at the old one -- the API's `update_listing` re-points the listing at a
 * new page while the old observations stay attached to the URL that produced them.
 */
export function EntryEdit() {
  const { id } = useParams();
  const entryId = Number(id);
  const navigate = useNavigate();
  const [entry, setEntry] = useState<ProductEntry | null>(null);
  const [name, setName] = useState("");
  const [fields, setFields] = useState<
    Record<number, { product_name: string; url: string }>
  >({});
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.getEntry(entryId).then((e) => {
      setEntry(e);
      setName(e.product_name);
      const f: Record<number, { product_name: string; url: string }> = {};
      for (const l of e.listings.filter((x) => x.is_active)) {
        f[l.id] = { product_name: l.product_name, url: l.url };
      }
      setFields(f);
    });
  }, [entryId]);

  if (!entry) return <p className="lede">Loading…</p>;
  const active = entry.listings.filter((l) => l.is_active);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !entry) return;
    setBusy(true);
    setErrors([]);
    try {
      if (name.trim() && name !== entry.product_name) {
        await api.renameEntry(entry.id, name.trim());
      }
      for (const l of active) {
        const f = fields[l.id];
        const patch: { product_name?: string; url?: string } = {};
        if (f.product_name.trim() && f.product_name !== l.product_name) {
          patch.product_name = f.product_name.trim();
        }
        if (f.url.trim() && f.url !== l.url) patch.url = f.url.trim();
        if (Object.keys(patch).length > 0) {
          await api.updateListing(entry.id, l.id, patch);
        }
      }
      navigate(`/products/${entry.id}`);
    } catch (err) {
      setErrors([err instanceof ApiError ? err.message : "That didn't go through."]);
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Edit product</h1>
      <p className="lede">
        Entry #{entry.id}. Editing never creates a second product, and changing a
        shop&rsquo;s link keeps everything already recorded at the old one.
      </p>

      {errors.length > 0 && (
        <div className="errors" role="alert">
          <strong>That didn&rsquo;t go through.</strong>
          <ul>
            {errors.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </div>
      )}

      <form className="card" onSubmit={save}>
        <label htmlFor="product_name">Product name</label>
        <input
          id="product_name"
          value={name}
          maxLength={200}
          onChange={(e) => setName(e.target.value)}
        />

        {active.map((l) => (
          <fieldset key={l.id}>
            <legend>{l.store_name}</legend>
            <label htmlFor={`name_${l.id}`}>{l.store_name} product name</label>
            <input
              id={`name_${l.id}`}
              value={fields[l.id]?.product_name ?? ""}
              maxLength={200}
              onChange={(e) =>
                setFields({
                  ...fields,
                  [l.id]: { ...fields[l.id], product_name: e.target.value },
                })
              }
            />
            <label htmlFor={`url_${l.id}`}>{l.store_name} URL</label>
            <input
              id={`url_${l.id}`}
              value={fields[l.id]?.url ?? ""}
              onChange={(e) =>
                setFields({
                  ...fields,
                  [l.id]: { ...fields[l.id], url: e.target.value },
                })
              }
            />
            <p className="hint">
              Must stay a {l.store_name} link. Changing it points this shop at a new page;
              the prices already recorded stay attached to the old one, because that is
              where they were seen.
            </p>
          </fieldset>
        ))}

        <div className="actions">
          <Link className="button ghost" to={`/products/${entry.id}`}>
            Cancel
          </Link>
          <button type="submit" className="button primary" disabled={busy}>
            {busy ? "Saving…" : "Save changes"}
          </button>
        </div>
      </form>
    </>
  );
}
