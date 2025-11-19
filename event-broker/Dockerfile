# Event Broker Service Dockerfile
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (including gcc for dependency-injector)
RUN apt-get update && apt-get install -y \
    gcc \
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
COPY event-broker/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY event-broker/app/ ./app/

# Set Python path and timezone
ENV PYTHONPATH=/app
ENV TZ=America/New_York

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import asyncio; from app.main import app; print('healthy' if asyncio.run(app.get_status()).get('running', False) else 'unhealthy')" || exit 1

# Command to run the application
CMD ["python", "-m", "app.main"]