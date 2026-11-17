# Reproducible local/CI image for SoundMap.
#
# It lives in docker/ rather than the repo root on purpose: a root Dockerfile
# makes Railway switch away from Nixpacks, which would change how production
# is built. Build with:
#   docker build -f docker/Dockerfile -t soundmap .

# ---- stage 1: build wheels so the runtime image needs no compilers ----------
FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.lock ./
RUN pip wheel --wheel-dir /wheels -r requirements.lock

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 soundmap

COPY --from=build /wheels /wheels
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.lock \
 && rm -rf /wheels /tmp/requirements.lock

WORKDIR /app
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Map storage when Supabase is not configured; owned by the app user so the
# container can run unprivileged.
RUN mkdir -p /app/user_data && chown -R soundmap:soundmap /app
USER soundmap

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
