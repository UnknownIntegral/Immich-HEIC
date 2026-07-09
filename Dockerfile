FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      exiftool \
      imagemagick \
      libheif-examples \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY assets ./assets

VOLUME ["/state"]
EXPOSE 8080

CMD ["python", "-m", "immich_heic_jpg"]
