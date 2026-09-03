import { render } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { AppRoutes } from "../routes";

/** Records the current pathname so a test can assert navigation without the data router. */
function LocationProbe({ onChange }: { onChange: (path: string) => void }) {
  const loc = useLocation();
  useEffect(() => onChange(loc.pathname + loc.search), [loc, onChange]);
  return null;
}

/**
 * Mount the real route tree at a starting path, so navigation between pages is exercised
 * end to end. Returns a `router`-shaped object with `state.location.pathname` to keep the
 * assertions in the tests readable.
 */
export function renderApp(initialPath = "/products") {
  const state = { location: { pathname: initialPath, search: "" } };
  const utils = render(
    <MemoryRouter initialEntries={[initialPath]}>
      <LocationProbe
        onChange={(p) => {
          const [pathname, search = ""] = p.split("?");
          state.location = { pathname, search: search ? `?${search}` : "" };
        }}
      />
      <AppRoutes />
    </MemoryRouter>,
  );
  return { router: { state }, ...utils };
}
