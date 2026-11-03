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
    docs, texts = load_documents(data_dir, 4)
    text = texts[docs[0]["doc_id"]]

    cases = {
        "classify": classify_body(text),
        "extract:termination": extract_body(text, "termination"),
        "extract:data_protection": extract_body(text, "data protection"),
        "risk": risk_body(
            {
                "termination": "either party may terminate",
                "governing_law": "laws of Ireland",
                "liability_cap": "EUR 1,000,000",
                "data_protection": None,
            },
            False,
        ),
        "report": report_body(
            [
                "- "
                + json.dumps(
                    {
                        "ref": d["doc_id"],
                        "type": d["type"],
                        "risk": d["risk"],
                        "governing_law": "laws of Ireland",
                        "data_protection": True,
                        "sensitive": d["sensitive"],
                    }
                )
                for d in docs
            ],
            4,
        ),
    }
    async with httpx.AsyncClient(base_url=base_url, timeout=120) as client:
        for name, body in cases.items():
            r = await client.post("/v1/chat/completions", json=body)
            print(
                f"{name:24} label={r.headers.get('x-demo-classification')!r:12} "
                f"route={r.headers.get('x-demo-route')!r} status={r.status_code}"
            )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080",
                     sys.argv[2] if len(sys.argv) > 2 else "data/synthetic"))
