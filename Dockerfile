FROM python:3.11-slim as base

# Install system dependencies (ffmpeg for ffprobe, curl, git)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    git \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY media_manager/ media_manager/
COPY main.py .

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8088

ENV PYTHONUNBUFFERED=1

CMD ["python3", "main.py", "serve", "--host", "0.0.0.0", "--port", "8088"]
