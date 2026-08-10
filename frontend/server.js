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
  createProxyMiddleware({ target: BACKEND_INTERNAL_URL, changeOrigin: true })
);

app.use(express.static(path.join(__dirname, "public")));

// Lets the browser know where the FastAPI backend lives without hardcoding it in the HTML.
app.get("/config", (req, res) => {
  res.json({ apiBaseUrl: API_BASE_URL });
});

app.listen(PORT, () => {
  console.log(`Wildfire monitor frontend listening on port ${PORT}`);
});
