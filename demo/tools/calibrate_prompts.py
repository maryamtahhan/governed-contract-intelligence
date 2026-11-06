import asyncio
import json
import sys

import httpx

from hybrid_demo.pipeline.review import (
    classify_body,
    extract_body,
    load_documents,
    report_body,
    risk_body,
)


async def main(base_url: str, data_dir: str) -> None:
    docs, texts, clause_keys = load_documents(data_dir, 4)
    if not docs:
        raise SystemExit(f"no documents in {data_dir}")
    text = texts[docs[0]["doc_id"]]
    has_data_protection = "data_protection" in clause_keys
    clauses = {key: None for key in clause_keys}
    clauses.update({
        "termination": "either party may terminate",
        "governing_law": "laws of Ireland",
        "liability_cap": "EUR 1,000,000",
    })

    cases = {
        "classify": classify_body(text),
        **{f"extract:{key}": extract_body(text, key.replace("_", " "))
           for key in clause_keys},
        "risk": risk_body(clauses, False),
        "report": report_body(
            [
                "- "
                + json.dumps(
                    {
                        "ref": d["doc_id"],
                        "type": d["type"],
                        "risk": d["risk"],
                        "governing_law": "laws of Ireland",
                        "data_protection": bool(
                            (d.get("clauses") or {}).get("data_protection")
                        ),
                        "sensitive": bool(d.get("sensitive")),
                    }
                )
                for d in docs
            ],
            len(docs),
            has_data_protection,
        ),
    }
    # The gateway's policy filter denies keyless requests; present the eval
    # key (digest in manifests/base/praxis/api-keys.yaml).
    from hybrid_demo.common import DEMO_API_KEYS

    async with httpx.AsyncClient(base_url=base_url, timeout=120,
                                 headers={"X-API-Key": DEMO_API_KEYS["eval"]}) as client:
        for name, body in cases.items():
            r = await client.post("/v1/chat/completions", json=body)
            print(
                f"{name:24} label={r.headers.get('x-demo-classification')!r:12} "
                f"route={r.headers.get('x-demo-route')!r} status={r.status_code}"
            )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080",
                     sys.argv[2] if len(sys.argv) > 2 else "data/synthetic"))
