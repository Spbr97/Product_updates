import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, type Group } from "../api";

/** Every comparison group this account owns, and a form to add one. */
export function GroupList() {
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [brand, setBrand] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .listGroups()
      .then((rows) => live && setGroups(rows))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.createGroup({
        name: name.trim(),
        ...(brand.trim() ? { brand: brand.trim() } : {}),
      });
      setGroups((rows) => [...(rows ?? []), created]);
      setName("");
      setBrand("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Comparisons</h1>
          <p className="lede">
            A group puts one product's models down the side and every shop across the top.
          </p>
        </div>
      </div>

      {error && (
        <div className="errors" role="alert">
          {error}
        </div>
      )}

      <form className="card inline-form" onSubmit={submit}>
        <div className="field">
          <label htmlFor="group-name">Name</label>
          <input
            id="group-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Galaxy S25"
            required
          />
        </div>
        <div className="field">
          <label htmlFor="group-brand">Brand (optional)</label>
          <input
            id="group-brand"
            value={brand}
            onChange={(e) => setBrand(e.target.value)}
            placeholder="Samsung"
          />
        </div>
        <button className="button primary" type="submit" disabled={busy}>
          {busy ? "Creating…" : "Create group"}
        </button>
      </form>

      {groups !== null && groups.length === 0 && (
        <div className="card empty">
          <p>No comparison groups yet.</p>
          <p className="hint">
            Create one above, then attach tracked listings to it from the CLI with{" "}
            <code>product-tracker groups attach</code>.
          </p>
        </div>
      )}

      {groups !== null && groups.length > 0 && (
        <table className="listing">
          <thead>
            <tr>
              <th scope="col">Group</th>
              <th scope="col">Brand</th>
              <th scope="col">Models</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <tr key={group.slug}>
                <th scope="row">
                  <Link to={`/compare/${group.slug}`}>{group.name}</Link>
                </th>
                <td>{group.brand ?? <span className="muted">—</span>}</td>
                <td>{group.variants.length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
