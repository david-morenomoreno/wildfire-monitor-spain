const express = require("express");
const path = require("path");
const { createProxyMiddleware } = require("http-proxy-middleware");

const app = express();
const PORT = process.env.PORT || 3000;
// Browser-facing base URL for API calls. Empty by default so the browser calls
// this same origin (port 3000) and the proxy below forwards to the backend
// container - avoids needing the backend's port publicly reachable. Set
// API_BASE_URL explicitly only if the backend must be called directly.
const API_BASE_URL = process.env.API_BASE_URL || "";
// Address to reach the backend from *inside* this container/network.
const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || "http://backend:8000";

app.use(
  ["/api", "/media"],
  createProxyMiddleware({
    target: BACKEND_INTERNAL_URL,
    changeOrigin: true,
    // Default error handler responds with a plain-text "Error occurred while
    // trying to proxy: ..." body - every page that does response.json() on
    // an /api call (sources.html, app.js, ranking.js) then crashes on
    // "Unexpected token 'E' ... is not valid JSON" instead of showing what
    // actually went wrong (backend unreachable/restarting). JSON here lets
    // callers show a real message instead.
    onError: (err, req, res) => {
      console.error(`Proxy error reaching backend (${BACKEND_INTERNAL_URL}):`, err.message);
      if (!res.headersSent) {
        res.writeHead(502, { "Content-Type": "application/json" });
      }
      res.end(JSON.stringify({ error: "backend_unreachable", detail: err.message }));
    },
  })
);

app.use(express.static(path.join(__dirname, "public")));

// Lets the browser know where the FastAPI backend lives without hardcoding it in the HTML.
app.get("/config", (req, res) => {
  res.json({ apiBaseUrl: API_BASE_URL });
});

app.listen(PORT, () => {
  console.log(`Wildfire monitor frontend listening on port ${PORT}`);
});
