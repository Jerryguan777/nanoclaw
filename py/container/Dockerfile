# NanoClaw Agent Container (Python)
# Runs Claude Agent SDK in isolated Linux VM with browser automation

FROM python:3.12-slim

# Install system dependencies for Chromium
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-liberation \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    libgbm1 \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libcups2 \
    libdrm2 \
    libxshmfence1 \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set Chromium path for agent-browser
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Install Python agent SDK (bundles Claude Code CLI)
RUN pip install --no-cache-dir claude-agent-sdk croniter

# Create app directory
WORKDIR /app

# Copy agent runner code
COPY agent_runner/ ./agent_runner/

# Create workspace directories
RUN mkdir -p /workspace/group /workspace/global /workspace/extra \
    /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input

# Create non-root user for sandbox
RUN useradd -m -s /bin/bash agent || true
RUN chown -R agent:agent /workspace && chmod 777 /home/agent

# Switch to non-root user
USER agent

# Set working directory to group workspace
WORKDIR /workspace/group

# Entry point reads JSON from stdin, outputs JSON to stdout
ENTRYPOINT ["python", "-m", "agent_runner"]
