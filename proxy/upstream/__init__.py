"""Upstream executors: the thing the proxy calls once a decision is `allow`.

``base.py`` (this Agent A) defines the ``UpstreamExecutor`` protocol every
backend implements. ``fake.py`` (this Agent A) is the in-process, no-network
default used by tests and by the app when the real factory isn't importable.
``cached.py``, ``mcp_client.py``, and ``factory.py`` (Agent B) select between
a recorded-response replay and the live Razorpay MCP server by ``DEMO_MODE``.
"""
