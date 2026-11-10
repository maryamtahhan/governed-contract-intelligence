"""Launcher for the MCP facade (design §3.5).

The implementation lives in the package so the console script, tests and the
wheel all use one module. Run it from the demo environment:

    cd demo && uv run --extra mcp python ../mcp/contract_review_server.py

Configure the governed ingress URLs first (never a pool's vLLM endpoint):

    HYBRID_GATEWAY=http://<trace-ui-general-host>
    HYBRID_SENSITIVE_GATEWAY=http://<trace-ui-sensitive-host>

Praxis chooses the pool from X-Demo-Step; the MCP facade only picks the
general or sensitive entry from the document's sensitivity.
"""

from hybrid_demo.mcp.server import main

if __name__ == "__main__":
    main()
