# NanoClaw Agent Container (Python)
# Runs Claude Agent SDK in an isolated Linux container

FROM python:3.12-slim

# Install essential tools only
# Chromium is NOT included — agent-browser is a Node.js package with no Python equivalent.
# WebSearch/WebFetch work without a browser (built into Claude Code CLI).
# If browser automation is needed in the future, add playwright + chromium.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python agent SDK (bundles Claude Code CLI)
RUN pip install --no-cache-dir claude-agent-sdk croniter

# Create app directory
WORKDIR /app

# Copy agent runner code
COPY src/agent_runner/ ./agent_runner/

# Create workspace directories
RUN mkdir -p /workspace/group /workspace/global /workspace/extra \
    /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input

# Create non-root user for sandbox
RUN useradd -m -s /bin/bash agent || true
RUN chown -R agent:agent /workspace && chmod 777 /home/agent

# Switch to non-root user
USER agent

# Ensure agent_runner is importable from any working directory
ENV PYTHONPATH=/app

# Set working directory to group workspace
WORKDIR /workspace/group

# Entry point reads JSON from stdin, outputs JSON to stdout
ENTRYPOINT ["python", "-m", "agent_runner"]
