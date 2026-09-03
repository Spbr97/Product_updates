/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// The build lands inside the Python package so the API can serve it as static files
// at /ui with no extra deployment. `base` matches that mount point, so asset URLs in
// index.html are absolute and correct however the SPA is entered.
export default defineConfig({
    plugins: [react()],
    base: "/ui/",
    build: {
        outDir: "../src/product_tracker/web/app",
        emptyOutDir: true,
    },
    server: {
        // `npm run dev` talks to a locally running API.
        proxy: { "/api": "http://127.0.0.1:8000" },
    },
    test: {
        environment: "jsdom",
        globals: true,
        setupFiles: ["./src/test/setup.ts"],
        css: true,
    },
});
