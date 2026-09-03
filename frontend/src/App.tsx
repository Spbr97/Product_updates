import { Link, Outlet } from "react-router-dom";

/** The shell: a header, the routed page, and the standing reminder about honesty. */
export function App() {
  return (
    <>
      <header className="bar">
        <Link className="brand" to="/products">
          Product Tracker
        </Link>
        <nav>
          <Link to="/products">Products</Link>
          <Link to="/compare">Compare</Link>
          <Link className="primary" to="/products/new">
            Add product
          </Link>
        </nav>
      </header>

      <main>
        <Outlet />
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
