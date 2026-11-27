# Rebalancer application image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# gosu performs the one-way privilege drop after repairing the state volume.
RUN apt-get update && apt-get install -y \
    gcc \
    gosu \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# CRITICAL: Copy local packages first
COPY packages/app-config /tmp/app-config
COPY packages/broker-connector-base /tmp/broker-connector-base
COPY packages/rebalance-calculator /tmp/rebalance-calculator
COPY packages/ibkr-connector /tmp/ibkr-connector

# CRITICAL: Install packages in dependency order (app-config first, then broker-base, then others)
RUN pip install --no-cache-dir /tmp/app-config && \
    pip install --no-cache-dir /tmp/broker-connector-base && \
    pip install --no-cache-dir /tmp/rebalance-calculator && \
    pip install --no-cache-dir /tmp/ibkr-connector && \
    rm -rf /tmp/app-config /tmp/broker-connector-base /tmp/rebalance-calculator /tmp/ibkr-connector

# Verify package installation
RUN pip list | grep -E "(app-config|broker|rebalance|ibkr)"

# Copy requirements first for better caching
COPY rebalancer/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Fail the build if local and runtime dependencies are inconsistent.
RUN pip check

# Copy application code
COPY rebalancer/app/ ./app/
COPY rebalancer/entrypoint.sh /app/entrypoint.sh

# Set Python path and timezone
ENV PYTHONPATH=/app
ENV TZ=America/New_York

# Python and its process-pool children run as this account. The entrypoint is
# intentionally started as root only to repair a root-mounted volume.
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app

# The health endpoint covers the event loop and a lightweight SQLite query.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD gosu appuser python -c 'import os,urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.getenv("PORT", "18778") + "/healthz", timeout=5).read()'

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
