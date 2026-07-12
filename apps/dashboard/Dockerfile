# agent-maker-checker dashboard — Vite static build, served by nginx on :3000
# (matches docker-compose.yml's `dashboard` service port mapping "3000:3000").

# ---- build stage ----------------------------------------------------------
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

# ---- runtime stage ---------------------------------------------------------
FROM nginx:alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Runs automatically before nginx starts (see docker-entrypoint.sh for why).
COPY docker-entrypoint.sh /docker-entrypoint.d/40-dashboard-env.sh
RUN chmod +x /docker-entrypoint.d/40-dashboard-env.sh

EXPOSE 3000
