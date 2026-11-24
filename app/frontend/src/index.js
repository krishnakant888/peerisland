'use strict';

// ── Frontend server ───────────────────────────────────────────────────────────
//
// PURPOSE: Express static file server for the frontend SPA.
//
// ROUTES:
//   /health → JSON health check (required by Kubernetes readiness/liveness probes
//             and the Docker HEALTHCHECK in the Dockerfile)
//   /*      → returns public/index.html for all unknown paths — this is the
//             "HTML5 pushState" pattern so a user navigating to /tasks directly
//             gets the SPA, not a 404
//
// WHY Express instead of pure nginx?
//   Express lets this service expose /health identically to the backend, making
//   the Kubernetes health check configuration symmetric.  An nginx static server
//   would need a custom nginx.conf to handle the /* → index.html fallback.
//   For a production frontend at scale, switch to multi-stage Dockerfile:
//   build React → copy dist/ into nginx:alpine → serve directly.
//
// NO TRACING here (unlike backend/telemetry.js):
//   The frontend server only serves static files — no business logic, no
//   database calls, no outgoing service calls.  Tracing it would add noise
//   with no insight.  The browser-side React app would instrument frontend
//   traces separately via @opentelemetry/sdk-web.
//
const express = require('express');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.static(path.join(__dirname, '..', 'public')));

app.get('/health', (_req, res) => {
  res.json({ status: 'healthy', service: 'peerisland-frontend', timestamp: new Date().toISOString() });
});

app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, '..', 'public', 'index.html'));
});

app.listen(PORT, () => console.log(`Frontend listening on :${PORT}`));
