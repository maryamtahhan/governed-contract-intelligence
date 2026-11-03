from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .risk import assess_risk

CONTRACT_TYPES = ["MSA", "NDA", "SOW", "DPA"]
TYPE_HEADINGS = {
    "MSA": "MASTER SERVICES AGREEMENT",
    "NDA": "NON-DISCLOSURE AGREEMENT",
    "SOW": "STATEMENT OF WORK",
    "DPA": "DATA PROCESSING AGREEMENT",
}

TERMINATION_VARIANTS = [
    "Either party may terminate this Agreement upon ninety (90) days' prior written notice.",
    "Either party may terminate this Agreement upon sixty (60) days' prior written notice to the other party.",
    "This Agreement may be terminated by either party for any reason upon thirty (30) days' written notice.",
    "This Agreement may be terminated only for material breach that remains uncured for thirty (30) days "
    "after written notice.",
]
GOVERNING_LAW_VARIANTS = [
    "This Agreement shall be governed by the laws of Ireland.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "This Agreement shall be governed by the laws of the State of New York.",
    "This Agreement shall be governed by the laws of Germany.",
]
LIABILITY_VARIANTS = [
    "The Supplier's aggregate liability shall not exceed the total fees paid or payable in the twelve (12) "
    "months preceding the claim.",
    "The Supplier's aggregate liability shall not exceed EUR 1,000,000 in the aggregate.",
    "The Supplier's aggregate liability shall not exceed the fees paid in the six (6) months preceding the claim.",
    "The Supplier's liability for any claim arising under this Agreement shall be unlimited.",
]
DATA_PROTECTION_VARIANTS = [
    "The Supplier shall process client information in accordance with applicable data protection law and "
    "shall maintain a data processing agreement.",
    "Each party shall comply with its obligations under applicable data protection legislation in respect "
    "of any client information received under this Agreement.",
]
CLAUSE_KEYS = ["termination", "governing_law", "liability_cap", "data_protection"]

SUPPLIERS = [
    "Northwind Analytics Ltd",
    "Vistaview Systems GmbH",
    "Harbourline Software DAC",
    "Copperfield Data Services Inc",
    "Silverthorn Cloud BV",
]
CLIENTS = [
    "Ballymore Foods plc",
    "Kestrel Logistics Group",
    "Ardmore Financial Services",
    "Thornbury Health Systems",
    "Lakeshore Manufacturing Ltd",
    "Pemberton Retail Holdings",
    "Greystone Energy Partners",
    "Ravenscourt Media SA",
]
FIRST_NAMES = ["Aoife", "Brendan", "Ciara", "David", "Eoin", "Fiona", "Gavin", "Hannah", "Liam", "Niamh"]
LAST_NAMES = ["Byrne", "Casey", "Doherty", "Fitzgerald", "Gallagher", "Hurley", "Kavanagh", "Lombard"]
CITIES = ["Dublin", "Cork", "Galway", "Limerick", "Waterford"]


def risk_of(clauses: dict) -> str:
    """Synthetic truth under the shared rubric (see pipeline/risk.py)."""
    return assess_risk(clauses, has_data_protection=True)


def build_document(rng: random.Random, ref: str, index: int = 0) -> tuple[str, dict]:
    ctype = rng.choice(CONTRACT_TYPES)
    supplier = rng.choice(SUPPLIERS)
    client = rng.choice(CLIENTS)
    term_months = rng.choice([12, 24, 36, 48])
    clauses = {
        "termination": rng.choice(TERMINATION_VARIANTS),
        "governing_law": rng.choice(GOVERNING_LAW_VARIANTS),
        "liability_cap": rng.choice(LIABILITY_VARIANTS),
        "data_protection": None if (rng.random() < 0.15 or index % 11 == 5) else rng.choice(DATA_PROTECTION_VARIANTS),
    }
    sensitive = rng.random() < 0.12 or index % 9 == 3
    month = rng.randint(1, 12)
    year = rng.choice([2025, 2026])

    lines = [
        TYPE_HEADINGS[ctype],
        f"Contract Reference: {ref}",
        f"Effective Date: {year}-{month:02d}-01   Initial Term: {term_months} months",
        f'Between: {supplier} ("Supplier") and {client} ("Client")',
        "",
        "CLAUSES",
        f"Termination: {clauses['termination']}",
        f"Governing Law: {clauses['governing_law']}",
        f"Liability Cap: {clauses['liability_cap']}",
    ]
    if clauses["data_protection"]:
        lines.append(f"Data Protection: {clauses['data_protection']}")
    else:
        lines.append("(No data-protection clause is present in this Agreement.)")
    if sensitive:
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        email = f"{name.split()[0].lower()}.{name.split()[1].lower()}@{client.split()[0].lower()}.example"
        phone = f"+353 1 555 {rng.randint(1000, 9999)}"
        emp1 = f"EMP-{rng.randint(100000, 999999)}"
        emp2 = f"EMP-{rng.randint(100000, 999999)}"
        lines += [
            "",
            "SCHEDULE 3 - PERSONAL DATA",
            f"Data Controller contact: {name}, {email}, {phone}",
            f"Data Controller address: {rng.randint(1, 200)} {rng.choice(CITIES)} Main Street, {rng.choice(CITIES)}",
            f"Employee IDs: {emp1}, {emp2}",
        ]
    text = "\n".join(lines) + "\n"

    truth = {
        "doc_id": ref,
        "type": ctype,
        "sensitive": sensitive,
        "initial_term_months": term_months,
        "clauses": clauses,
        "risk": risk_of(clauses),
    }
    return text, truth


def generate(out_dir: str | Path, count: int, seed: int, golden: int = 20) -> dict:
    rng = random.Random(seed)
    out = Path(out_dir)
    docs_dir = out / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)

    truths = []
    for i in range(count):
        ref = f"C-2026-{i + 1:04d}"
        text, truth = build_document(rng, ref, i)
        (docs_dir / f"{ref}.txt").write_text(text)
        truths.append(truth)

    manifest = {
        "seed": seed,
        "count": count,
        "golden": [t["doc_id"] for t in truths[:golden]],
        "documents": truths,
    }
    (out / "ground_truth.json").write_text(json.dumps(manifest, indent=1))
    sensitive = sum(1 for t in truths if t["sensitive"])
    return {
        "count": count,
        "golden": min(golden, count),
        "sensitive": sensitive,
        "missing_dp": sum(1 for t in truths if not t["clauses"]["data_protection"]),
        "risk_counts": {
            lvl: sum(1 for t in truths if t["risk"] == lvl) for lvl in ("low", "medium", "high")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="generate synthetic contracts + ground truth")
    parser.add_argument("--out", default="data/synthetic")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--golden", type=int, default=20)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} exists; use --force to regenerate")
        return
    stats = generate(out, args.count, args.seed, args.golden)
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
