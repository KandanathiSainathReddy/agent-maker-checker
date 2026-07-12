# Nova agent chat server — wraps worker.NovaWorker as an HTTP service for the
# dashboard playground (server.py). Runs as its own compose/Lambda service,
# separate from the enforcement proxy image (proxy/Dockerfile); it needs real
# AWS credentials at runtime to call Bedrock — the orchestrator wires those in.
#
# Build context is amplify/functions/agent/ (this directory). We copy it in AS
# the `agent` package at /app/agent (preserving the package layout, including
# __init__.py) so worker.py's relative imports (`from .provenance import ...`)
# resolve exactly as they do under pytest. WORKDIR is set to /app/agent so
# `uvicorn server:app` finds server.py as a top-level module (uvicorn inserts
# the cwd at sys.path[0]); PYTHONPATH=/app additionally makes `agent.*`
# absolute imports (used inside server.py) resolve against the package one
# level up.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app/agent

COPY requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

COPY . /app/agent/

EXPOSE 8100

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8100"]
