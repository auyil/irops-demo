FROM python:3.11-slim

WORKDIR /app

# System deps — sqlite3 CLI useful for debug, curl for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cache — only busts on requirements change)
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code as the 'api' package
COPY api/ ./api/

# Ensure data/skills directory exists (policy_chunks.json lives here)
RUN mkdir -p /app/api/data/skills

# SQLite DB lives in /data — mounted as a host volume for persistence across rebuilds
RUN mkdir -p /data
ENV IROPS_DB_PATH=/data/irops.db

# Bedrock region — must match where your IAM role has Bedrock access
ENV AWS_DEFAULT_REGION=us-east-1

# Expose FastAPI port
EXPOSE 8000

# Health check — nginx waits for this before routing traffic
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
