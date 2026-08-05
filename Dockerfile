FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reelgrab/ ./reelgrab/

ENV REELGRAB_DATA=/data
ENV REELGRAB_DOCKER=1
ENV PYTHONUNBUFFERED=1

VOLUME ["/data"]
RUN mkdir -p /data

CMD ["python", "-m", "reelgrab"]
