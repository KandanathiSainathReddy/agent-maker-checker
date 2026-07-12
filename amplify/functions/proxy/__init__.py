"""agent-maker-checker enforcement proxy: FastAPI app + declarative policy engine.

Money is integer paise everywhere in this package. YAML policy files under
``policies/`` express amounts in INR for human readability; ``proxy.engine``
converts them to paise at load time (see ``proxy.engine._convert_params``).
"""
