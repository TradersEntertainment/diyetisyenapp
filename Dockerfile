FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# postgresql-client provides pg_dump for scheduled backups;
# ffmpeg converts TTS mp3 -> ogg/opus for Telegram voice notes
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/diyetisyen

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
