# syntax=docker/dockerfile:1
# NEVER BUILT by the authors (no container runtime was available): read it, build it, and expect to adjust it.

FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RFACTORY_DATA_DIR=/data
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin rf && mkdir /data && chown rf /data
WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/rfactory backend/rfactory
# editable on purpose: the API finds the built UI at <app>/frontend/dist relative to the source tree
RUN pip install --no-cache-dir -e "./backend[postgres]"
COPY --from=ui /ui/dist frontend/dist
USER rf
VOLUME /data
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8088/api/health', timeout=4).status == 200 else 1)"
# --proxy-headers only matters behind a reverse proxy that you trust (set FORWARDED_ALLOW_IPS accordingly)
CMD ["uvicorn", "rfactory.api.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8088", "--proxy-headers"]
