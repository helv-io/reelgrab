FROM python:3.13-slim

WORKDIR /app

# Runtime deps for yt-dlp merge / convert / probe / thumbnails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the package from pyproject (pulls yt-dlp, PyYAML, aiohttp, aiofiles).
COPY pyproject.toml README.md LICENSE ./
COPY reelgrab/ ./reelgrab/
RUN pip install --no-cache-dir .

ENV REELGRAB_DATA=/data
ENV REELGRAB_DOCKER=1
ENV PYTHONUNBUFFERED=1

VOLUME ["/data"]
RUN mkdir -p /data

CMD ["python", "-m", "reelgrab"]
