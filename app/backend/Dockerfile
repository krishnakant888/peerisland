FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production

FROM node:20-alpine AS runtime
WORKDIR /app

# Non-root user
RUN addgroup -g 1001 -S nodejs && adduser -S nodejs -u 1001

COPY --from=deps /app/node_modules ./node_modules
COPY src/ ./src/
COPY package.json ./

USER nodejs

EXPOSE 3001
ENV PORT=3001 NODE_ENV=production

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:3001/health || exit 1

CMD ["node", "src/index.js"]
