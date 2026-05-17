# Use the official Playwright Python image — Chromium + all .so deps preinstalled.
# This avoids manually tracking ~20 system libs (libnss3, libatk, libgbm, …)
# and is required for garmin_auth.py's headless OAuth/MFA flow.
# noble (Ubuntu 24.04) ships Python 3.12, required by garminconnect>=0.3.3.
# Don't downgrade to jammy (Python 3.10) — pip install will fail to resolve
# garminconnect's wheels.
FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

# Playwright base image lacks tzdata — without it, TZ=Australia/Melbourne
# falls back to UTC and the app shows wrong wall-clock times.
# DEBIAN_FRONTEND=noninteractive prevents the geographic-area prompt during install.
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (cached layer when only source changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source — FastHTML single-file UI + extracted helpers + backend
COPY coach_app.py chat_helpers.py coach_helpers.py \
     garmin_data.py garmin_auth.py user_config.py db.py review_tools.py \
     report_jobs.py VERSION ./
COPY prompts/ ./prompts/
COPY ui/ ./ui/
COPY review_builders/ ./review_builders/
COPY i18n/ ./i18n/

# Persistent data (SQLite DB + .garth_session) lives here.
# Mount a host directory or named volume to /data to persist across restarts.
ENV DATA_DIR=/data
ENV APP_PORT=8507
RUN mkdir -p /data
VOLUME /data

# FastHTML serves on 8507. Map a host port to this in docker-compose.
EXPOSE 8507

# Health check: hit the lock screen (always reachable, no auth required).
# Returns 200 when APP_PASSWORD is set (the password form), 303 when not
# (redirects to /). Either way, a non-zero exit means the process is dead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -fsS -o /dev/null -w "%{http_code}" http://localhost:8507/lock | \
        grep -qE "^(200|303)$" || exit 1

CMD ["python3", "coach_app.py"]
