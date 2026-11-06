import asyncio
import json
import sys

import httpx

SYSTEM = (
    "You are a contract-analysis assistant for a legal-ops team. "
    "Answer with compact JSON only, no prose."
)
DOC = """MASTER SERVICES AGREEMENT
Contract Reference: C-2026-0001
Effective Date: 2026-01-01   Initial Term: 24 months

CLAUSES
Termination: Either party may terminate this Agreement upon ninety (90) days' prior written notice.
Governing Law: This Agreement shall be governed by the laws of Ireland.
Liability Cap: The Supplier's aggregate liability shall not exceed EUR 1,000,000 in the aggregate.
"""

VARIATIONS = {
    "bare-simple": [{"role": "user", "content": "What is the capital of France?"}],
    "bare-simple+sys": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "What is the capital of France?"},
    ],
    "extract-full-doc": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Extract the termination clause from the following contract."
         f"\n\n{DOC}\nReturn JSON."},
    ],
    "extract-no-sys": [
        {"role": "user", "content": "Extract the termination clause from the following contract."
         f"\n\n{DOC}\nReturn JSON."},
    ],
    "risk-plain": [
        {"role": "user", "content": "Assess the overall risk of this contract given the extracted clauses: "
         + json.dumps({"termination": "either party may terminate", "data_protection": None})
         + ". Return JSON."},
    ],
    "risk-step-by-step": [
        {"role": "user", "content": "Assess the overall risk of this contract. Reason step by step about "
         "termination rights, the liability cap and data-protection coverage, weigh the tradeoffs, and judge "
         "the risk level. Extracted clauses: "
         + json.dumps({"termination": "either party may terminate", "data_protection": None}) + ". Return JSON."},
    ],
    "risk-design-language": [
        {"role": "user", "content": "Evaluate and judge the overall risk of this contract. Analyse the "
         "extracted clauses, weigh conflicting obligations, and recommend mitigations with justification. "
         "Extracted clauses: " + json.dumps({"termination": "either party may terminate"}) + ". Return JSON."},
    ],
    "report-instruction-first": [
        {"role": "user", "content": "Produce the cross-document risk report across all 4 documents. Compare and "
         "aggregate: count contracts by risk level and name the highest-risk contract.\n\n"
         "- {\"ref\": \"C-1\", \"risk\": \"low\"}\n- {\"ref\": \"C-2\", \"risk\": \"high\"}\nReturn JSON."},
    ],
    "report-instruction-last": [
        {"role": "user", "content": "- {\"ref\": \"C-1\", \"risk\": \"low\"}\n"
         "- {\"ref\": \"C-2\", \"risk\": \"high\"}\n\n"
         "Produce the cross-document risk report across all 4 documents. Compare and aggregate: count contracts "
         "by risk level and name the highest-risk contract. Return JSON."},
    ],
    "report-reasoning-words": [
        {"role": "user", "content": "Produce the cross-document risk report across all 4 documents. Synthesise "
         "and compare the per-document extractions, derive the aggregate risk counts, prove the ranking, and "
         "name the highest-risk contract.\n\n"
         "- {\"ref\": \"C-1\", \"risk\": \"low\"}\n- {\"ref\": \"C-2\", \"risk\": \"high\"}\nReturn JSON."},
    ],
    "report-many-docs": [
        {"role": "user", "content": "Produce the cross-document risk report across all 40 documents. Compare and "
         "aggregate: count contracts by risk level and name the highest-risk contract.\n\n"
         + "\n".join(f"- {{\"ref\": \"C-{i}\", \"risk\": \"low\"}}" for i in range(40)) + "\nReturn JSON."},
    ],
    "halting-proof": [
        {"role": "user", "content": "Prove that the halting problem is undecidable, then explain how Rice's "
         "theorem generalizes the result step by step."},
    ],
    "halting-proof+contract": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"{DOC}\n\nProve that the halting problem is undecidable, then explain how "
         "Rice's theorem generalizes the result step by step."},
    ],
}


async def main(base_url: str) -> None:
    # The gateway's policy filter denies keyless requests; present the eval
    # key (digest in manifests/base/praxis/api-keys.yaml).
    from hybrid_demo.common import DEMO_API_KEYS

    async with httpx.AsyncClient(base_url=base_url, timeout=180,
                                 headers={"X-API-Key": DEMO_API_KEYS["eval"]}) as client:
        for name, messages in VARIATIONS.items():
            body = {"model": "Semantic-Qwen", "temperature": 0, "max_tokens": 24, "messages": messages}
            r = await client.post("/v1/chat/completions", json=body)
            print(f"{name:28} label={r.headers.get('x-demo-classification')!r:12} "
                  f"route={r.headers.get('x-demo-route')!r}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"))
