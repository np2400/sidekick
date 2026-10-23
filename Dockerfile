# Sidekick image: the code plus everything it needs to run, identical on a Mac or an AWS server.
# Secrets and data are NOT inside: they're supplied at run time (env vars and the /data volume).
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SIDEKICK_DATA_DIR=/data

# tzdata: timezone rules for reminders and dates (the slim image doesn't include them).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first: Docker caches this layer, so code-only changes rebuild in seconds.
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

# Run as an unprivileged user, never root: if the app were ever compromised, it can't take over the server.
RUN useradd --create-home --uid 10001 sidekick && mkdir -p /data && chown sidekick /data
USER sidekick

EXPOSE 8000
# Docker checks this every 30s and marks the container unhealthy if Sidekick stops answering.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# One process on purpose: the reminder scheduler must run exactly once.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
