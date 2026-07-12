"""Create/delete the three DynamoDB tables against DDB_ENDPOINT_URL.

Schemas match infra/CONTRACTS.md §4 exactly. In production these tables are
created by Agent C's CDK/Amplify stack; this module exists so
``tests/test_velocity_concurrency.py`` (and anyone poking at DynamoDB Local
by hand) can stand the same schema up with no AWS account, against
``docker compose up -d dynamodb-local`` (host port 8001).

Run directly to bootstrap DynamoDB Local for manual testing:

    python -m proxy.ddb_bootstrap --endpoint-url http://localhost:8001
"""

from __future__ import annotations

import argparse
import contextlib
from typing import Any

STATE_TABLE_SCHEMA: dict[str, Any] = {
    "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
    "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
    "BillingMode": "PAY_PER_REQUEST",
}

AUDIT_TABLE_SCHEMA: dict[str, Any] = {
    "AttributeDefinitions": [
        {"AttributeName": "chain", "AttributeType": "S"},
        {"AttributeName": "seq", "AttributeType": "N"},
    ],
    "KeySchema": [
        {"AttributeName": "chain", "KeyType": "HASH"},
        {"AttributeName": "seq", "KeyType": "RANGE"},
    ],
    "BillingMode": "PAY_PER_REQUEST",
}

APPROVALS_TABLE_SCHEMA: dict[str, Any] = {
    "AttributeDefinitions": [{"AttributeName": "approval_id", "AttributeType": "S"}],
    "KeySchema": [{"AttributeName": "approval_id", "KeyType": "HASH"}],
    "BillingMode": "PAY_PER_REQUEST",
}


def _client(endpoint_url: str | None, region_name: str = "us-east-1"):
    import boto3

    kwargs: dict[str, Any] = {"region_name": region_name}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
        kwargs.setdefault("aws_access_key_id", "local")
        kwargs.setdefault("aws_secret_access_key", "local")
    return boto3.client("dynamodb", **kwargs)


def create_tables(
    *,
    endpoint_url: str | None = None,
    region_name: str = "us-east-1",
    state_table: str = "amc-state",
    audit_table: str = "amc-audit",
    approvals_table: str = "amc-approvals",
) -> None:
    """Idempotently create all three tables. TTL is enabled on state/audit per §4."""
    from botocore.exceptions import ClientError

    client = _client(endpoint_url, region_name)
    tables = {
        state_table: STATE_TABLE_SCHEMA,
        audit_table: AUDIT_TABLE_SCHEMA,
        approvals_table: APPROVALS_TABLE_SCHEMA,
    }
    for name, schema in tables.items():
        try:
            client.create_table(TableName=name, **schema)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceInUseException":
                raise

    waiter = client.get_waiter("table_exists")
    for name in tables:
        waiter.wait(TableName=name)

    # TTL attribute "ttl" per CONTRACTS §4 (idle-item cleanup, $0 idle demo cost).
    for name in (state_table, audit_table):
        # DynamoDB Local accepts but some versions no-op silently; never fatal either way.
        with contextlib.suppress(ClientError):
            client.update_time_to_live(
                TableName=name,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
            )


def delete_tables(
    *,
    endpoint_url: str | None = None,
    region_name: str = "us-east-1",
    state_table: str = "amc-state",
    audit_table: str = "amc-audit",
    approvals_table: str = "amc-approvals",
) -> None:
    from botocore.exceptions import ClientError

    client = _client(endpoint_url, region_name)
    for name in (state_table, audit_table, approvals_table):
        try:
            client.delete_table(TableName=name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint-url", default="http://localhost:8001")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--delete", action="store_true", help="tear down instead of create")
    args = parser.parse_args()

    if args.delete:
        delete_tables(endpoint_url=args.endpoint_url, region_name=args.region)
        print(f"deleted amc-* tables at {args.endpoint_url}")
    else:
        create_tables(endpoint_url=args.endpoint_url, region_name=args.region)
        print(f"created amc-* tables at {args.endpoint_url}")


if __name__ == "__main__":
    _main()
