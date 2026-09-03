import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";

type Fields = {
  product_name: string;
  amazon_name: string;
  amazon_url: string;
  flipkart_name: string;
  flipkart_url: string;
};

const EMPTY: Fields = {
  product_name: "",
  amazon_name: "",
  amazon_url: "",
  flipkart_name: "",
  flipkart_url: "",
};

/**
 * The Add Product form (SDD §51). One product, tracked at both shops.
 *
 * Two behaviours the spec is explicit about, and both easy to get wrong:
 * - Every field keeps its value when the submit is rejected. Retyping five fields because
 *   one was wrong is a small cruelty that stops people using a thing.
 * - The submit disables itself the moment it is pressed, so a double click cannot become
 *   a second product before the server's 409 comes back.
 */
export function AddProduct() {
  const navigate = useNavigate();
  const [f, setF] = useState<Fields>(EMPTY);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const set = (k: keyof Fields) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF({ ...f, [k]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;

    const missing = Object.values(f).some((v) => v.trim() === "");
    if (missing) {
      setErrors(["Every field is required."]);
      return;
    }

    setBusy(true);
    setErrors([]);
    try {
      const entry = await api.createEntry({
        product_name: f.product_name,
        amazon: { product_name: f.amazon_name, url: f.amazon_url },
        flipkart: { product_name: f.flipkart_name, url: f.flipkart_url },
      });
      navigate(`/products/${entry.id}`);
    } catch (err) {
      // The server's own message, verbatim. "Invalid input" would tell the user nothing
      // about which of the five fields to fix.
      setErrors([err instanceof ApiError ? err.message : "That didn't go through."]);
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Add product</h1>
      <p className="lede">
        One product, tracked at both shops. Each shop keeps its own price and its own
        history; nothing is averaged or merged.
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

      <form className="card" onSubmit={submit}>
        <label htmlFor="product_name">Product name</label>
        <input
          id="product_name"
          value={f.product_name}
          onChange={set("product_name")}
          maxLength={200}
          placeholder="Samsung Galaxy S25 256GB"
        />
        <p className="hint">What you call it. Yours, not the shop&rsquo;s.</p>

        <fieldset>
          <legend>Amazon</legend>
          <label htmlFor="amazon_name">Amazon product name</label>
          <input
            id="amazon_name"
            value={f.amazon_name}
            onChange={set("amazon_name")}
            maxLength={200}
          />
          <label htmlFor="amazon_url">Amazon product URL</label>
          <input
            id="amazon_url"
            value={f.amazon_url}
            onChange={set("amazon_url")}
            placeholder="https://www.amazon.in/dp/..."
          />
          <p className="hint">Must be an amazon.in link.</p>
        </fieldset>

        <fieldset>
          <legend>Flipkart</legend>
          <label htmlFor="flipkart_name">Flipkart product name</label>
          <input
            id="flipkart_name"
            value={f.flipkart_name}
            onChange={set("flipkart_name")}
            maxLength={200}
          />
          <label htmlFor="flipkart_url">Flipkart product URL</label>
          <input
            id="flipkart_url"
            value={f.flipkart_url}
            onChange={set("flipkart_url")}
            placeholder="https://www.flipkart.com/.../p/itm..."
          />
          <p className="hint">Must be a flipkart.com link.</p>
        </fieldset>

        <div className="actions">
          <button
            type="button"
            className="button ghost"
            onClick={() => navigate("/products")}
          >
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy}>
            {busy ? "Adding…" : "Add product"}
          </button>
        </div>
      </form>
    </>
  );
}
