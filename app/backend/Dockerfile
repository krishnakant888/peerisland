# ── Backend Dockerfile ────────────────────────────────────────────────────────
#
# PURPOSE: Builds the production image for the Node.js backend service.
#   Uploaded to ACR by GitHub Actions build-push.yml, pulled by AKS via
#   the managed identity AcrPull role.
#
# TWO-STAGE BUILD (multi-stage):
#   Stage 1 (deps): installs npm dependencies.  Uses the full node:20-alpine
#     image which has npm.  --only=production skips devDependencies (jest, etc.)
#     so the final image doesn't carry test tools.
#   Stage 2 (runtime): copies only node_modules and src/ from stage 1.
#     The npm binary is explicitly removed — it's not needed at runtime and
#     the bundled npm includes a tar parser with known CVEs.
#
# WHY alpine base? Minimal attack surface (no bash, curl, etc.) and small
#   image size.  The build layer with npm is discarded; only the final stage
#   is pushed.
#
# NON-ROOT USER (uid 1001): Kubernetes admission policies and Pod Security
#   Standards reject containers running as root.  The adduser/addgroup commands
#   create a system account; USER nodejs activates it before CMD runs.
#
# HEALTHCHECK: Kubernetes readiness/liveness probes also check /health, but
#   Docker's HEALTHCHECK lets Docker itself report the container status (useful
#   when running locally with docker-compose or docker run).
#
# PORTS: 3001 (backend) vs 3000 (frontend) — both are ClusterIP services inside
#   the cluster; nginx routes /api/* to backend:3001.
#
FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production

FROM node:20-alpine AS runtime
WORKDIR /app

# Non-root user; strip npm (not needed at runtime — removes bundled tar CVE)
RUN addgroup -g 1001 -S nodejs && adduser -S nodejs -u 1001 \
 && rm -rf /usr/local/lib/node_modules/npm \
           /usr/local/bin/npm /usr/local/bin/npx

COPY --from=deps /app/node_modules ./node_modules
COPY src/ ./src/
COPY package.json ./

USER nodejs

EXPOSE 3001
ENV PORT=3001 NODE_ENV=production

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:3001/health || exit 1

CMD ["node", "src/index.js"]
