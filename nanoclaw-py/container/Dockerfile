# NanoClaw Agent Container (Python Edition)
# Runs Claude Agent SDK in isolated Linux container with browser automation

FROM python:3.12-slim

# Install system dependencies for Chromium + Node.js (needed for agent-browser/claude-code)
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-liberation \
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

# Install Node.js (required for agent-browser and claude-code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Set Chromium path for agent-browser
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Install agent-browser and claude-code globally
RUN npm install -g agent-browser @anthropic-ai/claude-code

# Create app directory
WORKDIR /app

# Copy and install Python dependencies
COPY agent_runner/pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY agent_runner/ ./

# Create workspace directories
RUN mkdir -p /workspace/group /workspace/global /workspace/extra \
    /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input

# Create entrypoint script
RUN printf '#!/bin/bash\nset -e\n[ -f /workspace/env-dir/env ] && export $(cat /workspace/env-dir/env | xargs)\ncat > /tmp/input.json\npython -m agent_runner.main < /tmp/input.json\n' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Create non-root user
RUN useradd -m -s /bin/bash agent
RUN chown -R agent:agent /workspace

USER agent
WORKDIR /workspace/group

ENTRYPOINT ["/app/entrypoint.sh"]
