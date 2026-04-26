FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for psycopg.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Collected at deploy time so whitenoise can serve them.
RUN python manage.py collectstatic --noinput || true

EXPOSE 8000
CMD ["gunicorn", "playto.wsgi", "--bind", "0.0.0.0:8000", "--workers", "3"]
