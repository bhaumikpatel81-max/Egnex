FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so this layer caches until requirements change.
# Build context is the project root, so paths are relative to it.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source code
COPY backend/. .

# Copy frontend so the backend can serve index.html and assets at runtime
COPY frontend/ frontend/

# Non-root user (required by the deployment quality gate)
RUN useradd -m appuser
USER appuser

# App binds to 0.0.0.0 and reads PORT from the environment (not hard-coded)
ENV PORT=8080
# Absolute path to the bundled frontend inside the container.
# main.py reads this env var so the path is always correct regardless of
# how Python resolves __file__ relative paths.
ENV FRONTEND_DIR=/app/frontend
EXPOSE 8080

# Healthcheck (required by the deployment quality gate)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8080\")}/api/health')" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
