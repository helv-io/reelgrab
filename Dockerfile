FROM python:3.13-slim

WORKDIR /app

# Runtime deps for yt-dlp merge / convert / probe / thumbnails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the package from pyproject (yt-dlp, PyYAML, aiohttp, mautrix, python-olm).
COPY pyproject.toml README.md LICENSE ./
COPY reelgrab/ ./reelgrab/
RUN pip install --no-cache-dir . \
    && python -c "import olm, mautrix"

ENV REELGRAB_DATA=/data
ENV REELGRAB_DOCKER=1
ENV PYTHONUNBUFFERED=1

VOLUME ["/data"]
RUN mkdir -p /data

# /health is 503 until the homeserver answers, then 200.
# The check uses the default appservice port 29399.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:29399/health', timeout=4)"

CMD ["python", "-m", "reelgrab"]
