FROM node:24-alpine AS monitoring-ui-frontend

WORKDIR /app/src/product_components/monitoring_ui/frontend
COPY src/product_components/monitoring_ui/frontend/package*.json ./
RUN npm ci
COPY src/product_components/monitoring_ui/frontend/ ./
ARG VITE_UI_API_BASE_URL=/api
ENV VITE_UI_API_BASE_URL=${VITE_UI_API_BASE_URL}
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src
COPY README.md ./
COPY --from=monitoring-ui-frontend /app/src/product_components/monitoring_ui/frontend/dist ./src/product_components/monitoring_ui/frontend/dist

RUN useradd --create-home --shell /usr/sbin/nologin trader \
    && mkdir -p /app/logs \
    && chown -R trader:trader /app
USER trader

EXPOSE 8090
CMD [".venv/bin/python", "-m", "src.product_components.monitoring_ui.backend"]
