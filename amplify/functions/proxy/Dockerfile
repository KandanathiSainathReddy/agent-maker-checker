# FastAPI enforcement proxy — one image, two runtimes.
#
# Locally (docker compose): the AWS Lambda Web Adapter extension is inert, uvicorn
# just serves HTTP on :8000.
# In cloud (CDK DockerImageCode.fromImageAsset): Lambda loads the adapter as an
# extension and bridges the Lambda Runtime API to the same uvicorn server. No code
# fork — the exact bytes that pass tests locally are what runs in the Lambda.
#
# The build context is amplify/functions/proxy/ (this dir). We copy it in AS the
# `proxy` package at /app/proxy so the code's `from proxy.x import y` imports —
# the same prefix used in tests and in app.py's upstream try/except — resolve
# identically here (PYTHONPATH=/app, uvicorn proxy.app:app). Live-mode note: the
# Razorpay MCP server is stdio-only; DEMO_MODE=live spawns it per-call. To run
# live inside this container, bake its binary in and set RAZORPAY_MCP_BIN — see
# infra/razorpay-mcp.md §3. The default DEMO_MODE=cached needs none of that.
FROM python:3.12-slim

# Lambda Web Adapter (bring-your-own-web-server → Lambda, no handler rewrite).
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

# Razorpay's open-source MCP server binary, baked in so DEMO_MODE=live can spawn
# it per-call via RAZORPAY_MCP_BIN inside the Lambda (a Lambda can't run Docker).
# Its own image ships the static binary at /app/razorpay-mcp-server.
COPY --from=razorpay/mcp:8607a4d95e67c86db8201d728658eb5f00d790fe /app/razorpay-mcp-server /usr/local/bin/razorpay-mcp-server

ENV PORT=8000 \
    AWS_LWA_PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    POLICY_DIR=/app/proxy/policies

WORKDIR /app

# Install deps first (cached across code-only changes), then the code as a package.
COPY requirements.txt /app/proxy/requirements.txt
RUN pip install --no-cache-dir -r /app/proxy/requirements.txt

COPY . /app/proxy/

EXPOSE 8000
CMD ["uvicorn", "proxy.app:app", "--host", "0.0.0.0", "--port", "8000"]
