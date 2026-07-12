# FastAPI enforcement proxy — one image, two runtimes.
#
# Locally (docker compose): the AWS Lambda Web Adapter extension is inert, uvicorn
# just serves HTTP on :8000.
# In cloud (CDK DockerImageCode.fromImageAsset): Lambda loads the adapter as an
# extension and bridges the Lambda Runtime API to the same uvicorn server. No code
# fork — the exact bytes that pass tests locally are what runs in the Lambda.
FROM python:3.12-slim

# Lambda Web Adapter (bring-your-own-web-server → Lambda, no handler rewrite).
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8000 \
    AWS_LWA_PORT=8000 \
    POLICY_DIR=/app/policies \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
