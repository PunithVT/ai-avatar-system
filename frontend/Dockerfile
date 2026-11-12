# syntax=docker/dockerfile:1
#
# Three stages, so the image that runs carries only what running needs:
#   deps    — node_modules for the build (dev dependencies included)
#   builder — `next build` with output: 'standalone'
#   runner  — the standalone server, static assets, a non-root user
#
# The base must satisfy next@16's engines.node >=20.9.0 — on node:18
# `next build` refuses to run at all. Kept in step with NODE_VERSION in
# .github/workflows/ci.yml so the image and CI build on the same runtime.
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder
WORKDIR /app

# NEXT_PUBLIC_* values are inlined into the client bundle AT BUILD TIME —
# setting them only as runtime env on the container has no effect on the
# already-compiled JS. Accept them as build args (compose passes them via
# build.args) and export as ENV so `npm run build` sees them.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_WS_URL=
ARG NEXT_PUBLIC_SITE_URL=http://localhost:3000
ARG NEXT_PUBLIC_S3_BUCKET_DOMAIN=
ARG NEXT_PUBLIC_CLOUDFRONT_DOMAIN=
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL \
    NEXT_PUBLIC_SITE_URL=$NEXT_PUBLIC_SITE_URL \
    NEXT_PUBLIC_S3_BUCKET_DOMAIN=$NEXT_PUBLIC_S3_BUCKET_DOMAIN \
    NEXT_PUBLIC_CLOUDFRONT_DOMAIN=$NEXT_PUBLIC_CLOUDFRONT_DOMAIN \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The node image ships an unprivileged `node` user (uid 1000). Nothing here
# needs root at runtime.
#
# .next/standalone is a self-contained server with only the node_modules it
# actually traced as used — no dev dependencies, no source. Static assets are
# not part of the trace and are copied alongside it. There is no public/
# directory in this app; add a COPY for it here if one is ever created.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

USER node
EXPOSE 3000

# server.js is emitted by the standalone build; it replaces `next start`.
CMD ["node", "server.js"]
