"""The Nova-powered demo agent: a support/ops worker at a Razorpay merchant.

Everything in this package is the *client* of the enforcement proxy
(``amplify/functions/proxy``), never the enforcement itself — this package
decides what to attempt; ``proxy`` decides what is allowed to happen. See
``infra/CONTRACTS.md`` §1 for the frozen wire contract between the two.
"""
