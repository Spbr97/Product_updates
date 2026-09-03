import { Route, Routes } from "react-router-dom";
import { App } from "./App";
import { ProductList } from "./pages/ProductList";
import { AddProduct } from "./pages/AddProduct";
import { EntryDetail } from "./pages/EntryDetail";
import { EntryEdit } from "./pages/EntryEdit";
import { GroupList } from "./pages/GroupList";
import { GroupCompare } from "./pages/GroupCompare";

/**
 * The classic (non-data) router tree. No loaders or actions -- every page fetches in a
 * `useEffect` and posts on submit -- so the data-router API would only add an internal
 * `fetch` that jsdom's cross-realm `AbortSignal` chokes on in tests. Kept in one file so
 * `main.tsx` and the test harness mount exactly the same routes.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<App />}>
        <Route index element={<ProductList />} />
        <Route path="products" element={<ProductList />} />
        <Route path="products/new" element={<AddProduct />} />
        <Route path="products/:id" element={<EntryDetail />} />
        <Route path="products/:id/edit" element={<EntryEdit />} />
        <Route path="compare" element={<GroupList />} />
        <Route path="compare/:slug" element={<GroupCompare />} />
      </Route>
    </Routes>
  );
}
