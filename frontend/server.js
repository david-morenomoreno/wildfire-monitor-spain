const express = require("express");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 3000;
const API_BASE_URL = process.env.API_BASE_URL || "http://localhost:8000";

app.use(express.static(path.join(__dirname, "public")));

// Lets the browser know where the FastAPI backend lives without hardcoding it in the HTML.
app.get("/config", (req, res) => {
  res.json({ apiBaseUrl: API_BASE_URL });
});

app.listen(PORT, () => {
  console.log(`Wildfire monitor frontend listening on port ${PORT}`);
});
