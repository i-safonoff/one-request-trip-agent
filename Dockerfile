# syntax=docker/dockerfile:1
# Backend: the FastAPI host (chat, Telegram bot, history, agents API, UI) and the agents worker share this image
# (docker-compose runs two processes from it).

FROM python:3.12.16-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY backend/requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.12.16-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    TRIP_MAP_DATA_DIR=/data/trip_map
# no dev tools in the runtime image; unprivileged user; writable dir for the geocoder/route caches
RUN useradd --system --uid 10001 --home-dir /srv --shell /usr/sbin/nologin app \
 && mkdir -p /data/trip_map && chown -R app:app /data
COPY --from=builder /install /usr/local
WORKDIR /srv/backend
COPY --chown=app:app backend/main.py backend/integration.py backend/alembic.ini ./
COPY --chown=app:app backend/shared shared
COPY --chown=app:app backend/chat chat
COPY --chown=app:app backend/history history
COPY --chown=app:app backend/trip_map trip_map
COPY --chown=app:app backend/agents agents
COPY --chown=app:app backend/migrations migrations
COPY --chown=app:app backend/scripts/seed_history_demo.py scripts/seed_history_demo.py
USER app
EXPOSE 8080
# the database schema is migrated before the API starts (the worker waits for the API to be healthy)
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn main:app --host 0.0.0.0 --port 8080"]
