"""One pure module per policy: ``evaluate(ctx: PolicyContext) -> PolicyEvaluation``.

Pure over ``(request, state, config)`` in the sense that every input is
passed in through ``PolicyContext`` (no hidden globals, no reading the
filesystem or environment) and the only side effects are the explicit ones
made through the injected ``StateStore`` (recording spend, freezing a pair) —
never a network call, never a clock read (``ctx.now`` is injected too).
"""
