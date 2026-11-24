'use strict';

// ── Backend application entry point ──────────────────────────────────────────
//
// PURPOSE: Express HTTP server for a task management API.  This is the
//   "application" layer of the demo — its main purpose is to generate realistic
//   HTTP traffic so the observability stack (metrics, logs, traces) has real data.
//
// TELEMETRY STRATEGY — three pillars, all in this file:
//   Metrics:  prom-client manual instrumentation (httpRequestsTotal counter,
//             httpRequestDuration histogram).  Alloy scrapes /metrics.
//   Traces:   OTel auto-instrumentation via telemetry.js (required first).
//             Spans are created automatically for every express route.
//   Logs:     stdout (console.log) — Alloy collects these from the container log.
//
// WHY require('./telemetry') first?
//   OpenTelemetry works by monkey-patching Node.js modules (express, http, etc.)
//   at require() time.  If express is required before OTel is initialised, the
//   patches don't apply and no traces are captured.  Telemetry must be loaded
//   before any instrumented module.
//
// TRACE ID IN HEADER (X-Trace-Id):
//   The middleware reads the active OTel span's trace ID and injects it into
//   the response header.  Frontend logs or client-side tooling can correlate
//   a failed API call with the exact trace in Grafana Tempo.
//
// /metrics ENDPOINT:
//   Exposed for Alloy to scrape (prometheus pull model).  Returns all counters
//   and histograms registered in the prom-client Registry in Prometheus text format.
//   This is the source of http_requests_per_second and http_request_duration_p99
//   custom metrics that HPA uses for scaling decisions.
//
require('./telemetry');   // must be first — OTel patches modules at load time

const { trace, context } = require('@opentelemetry/api');
const express = require('express');
const cors = require('cors');
const client = require('prom-client');

const app = express();
const PORT = process.env.PORT || 3001;
const SERVICE_NAME = 'peerisland-backend';

const register = new client.Registry();
register.setDefaultLabels({ service: SERVICE_NAME });
client.collectDefaultMetrics({ register });

const httpRequestsTotal = new client.Counter({
  name: 'http_requests_total',
  help: 'Total number of HTTP requests',
  labelNames: ['method', 'route', 'status_code'],
  registers: [register],
});

const httpRequestDuration = new client.Histogram({
  name: 'http_request_duration_seconds',
  help: 'HTTP request latency in seconds',
  labelNames: ['method', 'route', 'status_code'],
  buckets: [0.05, 0.1, 0.3, 0.5, 1, 2, 5],
  registers: [register],
});

app.use(cors());
app.use(express.json());

app.use((req, res, next) => {
  const end = httpRequestDuration.startTimer();
  const span = trace.getActiveSpan();
  const traceId = span?.spanContext().traceId;
  if (traceId) res.setHeader('X-Trace-Id', traceId);
  res.on('finish', () => {
    const labels = { method: req.method, route: req.route?.path ?? req.path, status_code: res.statusCode };
    httpRequestsTotal.inc(labels);
    end(labels);
  });
  next();
});

const tasks = new Map();
let nextId = 1;

app.get('/health', (_req, res) => {
  res.json({ status: 'healthy', service: SERVICE_NAME, timestamp: new Date().toISOString() });
});

app.get('/api/tasks', (_req, res) => {
  res.json(Array.from(tasks.values()));
});

app.post('/api/tasks', (req, res) => {
  const { title, description = '' } = req.body;
  if (!title || typeof title !== 'string') {
    return res.status(400).json({ error: 'title is required' });
  }
  const task = { id: nextId++, title, description, done: false, createdAt: new Date().toISOString() };
  tasks.set(task.id, task);
  res.status(201).json(task);
});

app.put('/api/tasks/:id', (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!tasks.has(id)) return res.status(404).json({ error: 'Task not found' });
  const updated = { ...tasks.get(id), ...req.body, id };
  tasks.set(id, updated);
  res.json(updated);
});

app.delete('/api/tasks/:id', (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!tasks.has(id)) return res.status(404).json({ error: 'Task not found' });
  tasks.delete(id);
  res.status(204).send();
});

app.get('/metrics', async (_req, res) => {
  res.set('Content-Type', register.contentType);
  res.end(await register.metrics());
});

app.listen(PORT, () => console.log(`${SERVICE_NAME} listening on :${PORT}`));
