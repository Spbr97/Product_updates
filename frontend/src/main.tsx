import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { AppRoutes } from "./routes";
import "./styles.css";

// `basename` matches the API's static mount at /ui, so a deep link the server rewrites to
// index.html still routes correctly on the client.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename="/ui">
      <AppRoutes />
    </BrowserRouter>
  </React.StrictMode>,
);
