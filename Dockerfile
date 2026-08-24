FROM python:3.12-slim

WORKDIR /app

# gcc for the couple of Google client libs that build a wheel on install
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Precompile to .pyc at build time. Importing the tool registry means compiling
# ~3,000 tool functions; a warm __pycache__ was measured at 7.9s cold-start
# against 21.2s without it on the hosted deployment of this same tree.
RUN python -m compileall -q /app || true

ENV PORT=8000
EXPOSE ${PORT}

# HTTP transport inside a container; bind all interfaces INSIDE the container
# and let the operator decide what the host publishes. Set MCP_ADS_API_KEY.
ENV MCP_ADS_ALLOW_PUBLIC_BIND=true
CMD python server.py serve --transport http --host 0.0.0.0 --port ${PORT}
