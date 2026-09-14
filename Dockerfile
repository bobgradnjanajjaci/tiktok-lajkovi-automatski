# Railway detects a Dockerfile named exactly "Dockerfile" at the repository root.
# No browser, no Playwright, no Chromium: the chosen reader is a plain HTTP
# client, so the image stays small and starts fast.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is only here so the container can be probed by hand during troubleshooting.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
# Fixture samples ship so the demo dry run works inside the image.
COPY tests/fixtures ./tests/fixtures
COPY start.sh ./start.sh
# Optional: only the generic COMMENT_READER=http adapter reads a contract file.
# If the file exists in the repository it is copied to /app/reader_contract.json,
# which is the path the README tells you to configure. The default
# COMMENT_READER=scrapecreators adapter needs no file at all.
COPY reader_contract*.json ./

# Unix line endings and the executable bit, set inside the image so a Windows
# checkout cannot break the entrypoint.
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

# Default location for the SQLite file. On Railway this path must be a mounted
# volume, otherwise order history and duplicate protection are lost on redeploy.
ENV DATABASE_PATH=/data/app.db
RUN mkdir -p /data

EXPOSE 8080

# Shell form so ${PORT} is expanded, and start.sh execs uvicorn so that SIGTERM
# reaches the server process directly for a clean shutdown.
CMD ["/app/start.sh"]
