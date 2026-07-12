# Nova agent chat server — wraps worker.NovaWorker as an HTTP service for the
# dashboard playground (server.py). One image, two runtimes, same as the
# enforcement proxy image (proxy/Dockerfile):
#
# Locally (docker compose): the AWS Lambda Web Adapter extension is inert,
# uvicorn just serves HTTP on :8100.
# In cloud (CDK DockerImageCode.fromImageAsset): Lambda loads the adapter as an
# extension and bridges the Lambda Runtime API to the same uvicorn server. No
# code fork — the exact bytes that pass tests locally are what runs in the
# Lambda. It needs real AWS credentials at runtime to call Bedrock — the
# Lambda execution role supplies those in cloud; the orchestrator wires local
# creds in for docker compose.
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

# Lambda Web Adapter (bring-your-own-web-server → Lambda, no handler rewrite).
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8100 \
    AWS_LWA_PORT=8100 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app/agent

COPY requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

COPY . /app/agent/

EXPOSE 8100

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8100"]
