import { useState } from "react";
import { setApiKey } from "../api";

/**
 * Asked for when the API rejects our credentials.
 *
 * Without this the shell was a dead end: every page rendered "missing or invalid
 * X-API-Key" and offered no way to supply one, so a deployment with accounts was
 * unusable from the browser however correct the API was.
 *
 * It says where a key comes from, because accounts are provisioned from the CLI and
 * nothing in the browser can create one -- an error that does not tell you the next
 * command is only half an error message.
 */
export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [key, setKey] = useState("");

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = key.trim();
    if (!trimmed) return;
    setApiKey(trimmed);
    onSignedIn();
  }

  return (
    <div className="card signin">
      <h1>Sign in</h1>
      <p className="lede">
        This deployment has accounts, so the browser needs an API key before it can show
        you anything.
      </p>

      <form onSubmit={submit}>
        <label htmlFor="api-key">API key</label>
        <input
          id="api-key"
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="pt_…"
          autoComplete="current-password"
          autoFocus
        />
        <div className="actions">
          <button className="button primary" type="submit" disabled={!key.trim()}>
            Sign in
          </button>
        </div>
      </form>

      <p className="hint">
        Keys are issued from the command line — the API authenticates requests, it does
        not create accounts:
      </p>
      <pre className="snippet">
        <code>product-tracker users add you@example.com</code>
      </pre>
      <p className="hint">
        Already have an account but lost the key?{" "}
        <code>product-tracker users rotate-key you@example.com</code> issues a new one and
        invalidates the old immediately. The key is kept in this browser only.
      </p>
    </div>
  );
}
