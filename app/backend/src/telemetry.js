'use strict';

// ── OpenTelemetry SDK initialisation ─────────────────────────────────────────
//
// PURPOSE: Bootstraps the OpenTelemetry SDK so every HTTP request handled by
//   Express generates a distributed trace automatically.  This file must be
//   required BEFORE express (and all other instrumented modules) — OTel works
//   by patching require() at load time.
//
// DATA FLOW:
//   App → (OTLP gRPC) → Alloy DaemonSet → Tempo backend → Grafana Explore
//
//   OTEL_EXPORTER_OTLP_ENDPOINT is set by the Rollout spec (env var):
//     http://alloy.monitoring.svc.cluster.local:4317
//   The gRPC endpoint must NOT have http:// prefix — stripped here with replace().
//
// WHY NodeSDK (not manual span creation)?
//   getNodeAutoInstrumentations() auto-instruments express routes, http calls,
//   and dns lookups without touching application code.  Every incoming request
//   becomes a span; outgoing fetch() calls become child spans — building a full
//   call tree automatically.
//
// WHY disable instrumentation-fs?
//   The fs instrumentation creates a span for every filesystem operation —
//   including every require() call during startup.  That generates thousands
//   of noisy, low-value spans per startup.  Disabled to reduce Tempo storage.
//
// METRIC EXPORT via OTLPMetricExporter:
//   Also ships OTel-native metrics (not prom-client) to Alloy via the same
//   gRPC connection.  exportIntervalMillis: 15000 → push metrics every 15s.
//   These are separate from the /metrics Prometheus endpoint; both coexist.
//
// SHUTDOWN HANDLERS (SIGTERM, SIGINT):
//   Kubernetes sends SIGTERM to the container before stopping a pod.
//   sdk.shutdown() flushes any in-flight spans/metrics to the collector
//   before the process exits, preventing data loss during rolling updates.
//
// Must be required BEFORE any other imports — OTel patches Node.js modules at load time
const { NodeSDK } = require('@opentelemetry/sdk-node');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');
const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-grpc');
const { PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');
const { Resource } = require('@opentelemetry/resources');
const { SEMRESATTRS_SERVICE_NAME, SEMRESATTRS_SERVICE_VERSION, SEMRESATTRS_DEPLOYMENT_ENVIRONMENT } = require('@opentelemetry/semantic-conventions');
const { credentials } = require('@grpc/grpc-js');

const otlpEndpoint = process.env.OTEL_EXPORTER_OTLP_ENDPOINT || 'http://localhost:4317';
// gRPC endpoint must NOT have http:// prefix
const grpcEndpoint = otlpEndpoint.replace(/^https?:\/\//, '');

const resource = new Resource({
  [SEMRESATTRS_SERVICE_NAME]:            process.env.OTEL_SERVICE_NAME || 'peerisland-backend',
  [SEMRESATTRS_SERVICE_VERSION]:         process.env.npm_package_version || '1.0.0',
  [SEMRESATTRS_DEPLOYMENT_ENVIRONMENT]:  process.env.NODE_ENV || 'development',
});

const sdk = new NodeSDK({
  resource,
  traceExporter: new OTLPTraceExporter({
    url: grpcEndpoint,
    credentials: credentials.createInsecure(),
  }),
  metricReader: new PeriodicExportingMetricReader({
    exporter: new OTLPMetricExporter({
      url: grpcEndpoint,
      credentials: credentials.createInsecure(),
    }),
    exportIntervalMillis: 15000,
  }),
  instrumentations: [
    getNodeAutoInstrumentations({
      '@opentelemetry/instrumentation-fs': { enabled: false },  // too noisy
    }),
  ],
});

sdk.start();

process.on('SIGTERM', () => sdk.shutdown().finally(() => process.exit(0)));
process.on('SIGINT',  () => sdk.shutdown().finally(() => process.exit(0)));
