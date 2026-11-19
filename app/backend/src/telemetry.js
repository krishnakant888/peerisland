'use strict';

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
