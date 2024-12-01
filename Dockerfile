FROM ubuntu:22.04

# Set non-interactive mode for apt
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && \
    apt-get install -y curl git build-essential pkg-config python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Install Lean and Lake
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y
ENV PATH="/root/.elan/bin:/root/.elan/bin/lean/bin:${PATH}"
ENV WORKING_DIR=.

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Create app directory
WORKDIR /app

# Copy the API code
COPY main.py .

# Command to run the API
CMD uvicorn main:app --host 0.0.0.0 --port 8081 --access-log --log-level info
