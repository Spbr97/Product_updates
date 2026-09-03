import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./server";

// One MSW server for the whole run. Tests reach for `server.use(...)` to override a
// handler for a single case (a 409, a blocked shop) and `resetHandlers` puts the baseline
// back afterwards, so no test leaks state into the next.
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
