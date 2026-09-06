# ── Build stage ──────────────────────────────────
FROM python:3.11-slim AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Final stage (no gcc, no build tools) ─────────
FROM python:3.11-slim

WORKDIR /app

# Copy only installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY bot.py config.py ./
COPY core/ ./core/
COPY handlers/ ./handlers/
COPY utils/ ./utils/

# Create data directories
RUN mkdir -p data downloads

# Remove pip to save space
RUN pip uninstall -y pip setuptools wheel 2>/dev/null; true

CMD ["python", "bot.py"]
