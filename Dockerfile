FROM --platform=$BUILDPLATFORM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
WORKDIR /app
ARG VERSION=0.4.0
LABEL org.opencontainers.image.title="US3000 Power Monitor" \
      org.opencontainers.image.description="Read-only UGREEN US3000 dashboard for Linux NAS" \
      org.opencontainers.image.source="https://github.com/BSakura-Miku/ugreen-ups-panel" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UPS_STATIC=/app/static
COPY requirements.lock ./
COPY LICENSE THIRD_PARTY_NOTICES.txt ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home panel && mkdir /data && chown panel:panel /data
COPY ups_panel/ ./ups_panel/
COPY --from=frontend /build/dist ./static/
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"
CMD ["uvicorn", "ups_panel.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
