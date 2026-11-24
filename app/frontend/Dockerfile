# ── Frontend Dockerfile ───────────────────────────────────────────────────────
#
# PURPOSE: Builds the production image for the frontend service.  The frontend
#   is a Node.js Express server that serves static files from public/ — it's NOT
#   an nginx static server.  Chosen for simplicity and to demonstrate the same
#   healthcheck and non-root patterns as the backend.
#
# STRUCTURE matches backend Dockerfile exactly (two-stage, non-root, npm removed)
#   with two differences:
#   - port 3000 (backend is 3001)
#   - Copies public/ (static assets: HTML, CSS, JS) in addition to src/
#
# STATIC FILE SERVER pattern (src/index.js):
#   express.static serves everything in public/.
#   Catch-all route (*) returns public/index.html — SPA behaviour so React Router
#   (or any client-side router) handles path rendering without 404s on direct
#   URL access.
#
# In production, an nginx-based image would be more efficient for static serving,
#   but Express is used here to keep the Docker skill demonstration consistent
#   between frontend and backend.
#
FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production

FROM node:20-alpine AS runtime
WORKDIR /app

RUN addgroup -g 1001 -S nodejs && adduser -S nodejs -u 1001 \
 && rm -rf /usr/local/lib/node_modules/npm \
           /usr/local/bin/npm /usr/local/bin/npx

COPY --from=deps /app/node_modules ./node_modules
COPY src/ ./src/
COPY public/ ./public/
COPY package.json ./

USER nodejs

EXPOSE 3000
ENV PORT=3000 NODE_ENV=production

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:3000/health || exit 1

CMD ["node", "src/index.js"]
