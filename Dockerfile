FROM python:3.12-slim

# ffmpeg is needed for the GG TV bot stream feature
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Don't run as root
RUN useradd -m appuser && chown -R appuser /app
USER appuser

# Run DB migrations, then start gunicorn
CMD ["sh", "-c", "flask db upgrade && exec gunicorn 'app:app' --workers 4 --bind 0.0.0.0:8000 --timeout 120 --access-logfile - --error-logfile -"]
