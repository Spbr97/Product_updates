import { useCallback, useEffect, useState } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";
import { hasApiKey, setApiKey, setUnauthorizedHandler } from "./api";
import { SignIn } from "./pages/SignIn";

/** The shell: a header, the routed page, and the standing reminder about honesty. */
export function App() {
  const [locked, setLocked] = useState(false);
  const [signedIn, setSignedIn] = useState(hasApiKey);
  // Bumped on sign-in to remount the routed page so it refetches with the new key.
  const [nonce, setNonce] = useState(0);
  const location = useLocation();

  // One handler for the whole app: any page can be the first one loaded, so per-page
  // handling would be four copies that drift apart.
  useEffect(() => {
    setUnauthorizedHandler(() => setLocked(true));
    return () => setUnauthorizedHandler(null);
  }, []);

  const signedInNow = useCallback(() => {
    setSignedIn(true);
    setLocked(false);
    setNonce((n) => n + 1);
  }, []);

  function signOut() {
    setApiKey(null);
    setSignedIn(false);
    setLocked(true);
  }

  return (
    <>
      <header className="bar">
        <Link className="brand" to="/products">
          Product Tracker
        </Link>
        <nav>
          <Link to="/products">Products</Link>
          <Link to="/compare">Compare</Link>
          {signedIn && (
            <button className="button ghost small" onClick={signOut} type="button">
              Sign out
            </button>
          )}
          <Link className="primary" to="/products/new">
            Add product
          </Link>
        </nav>
      </header>

      <main>
        {locked ? (
          <SignIn onSignedIn={signedInNow} />
        ) : (
          <Outlet key={`${location.pathname}:${nonce}`} />
        )}
      </main>

      <footer className="foot">
        <p>
          Prices and stock are read from each shop separately. A shop we could not read is
          reported as unreadable &mdash; never as out of stock.
        </p>
      </footer>
    </>
  );
}
