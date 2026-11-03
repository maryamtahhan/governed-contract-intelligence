from __future__ import annotations

ILLUSTRATIVE_RATES = {
    "cloud": {"gpu_hour": 1.10, "core_hour": 0.13},
    "owned": {"gpu_hour": 0.75, "core_hour": 0.03, "utilisation": [0.25, 0.50, 0.75]},
}


def per_document(gpu_seconds: float, cpu_core_seconds: float, docs: int) -> dict:
    docs = max(docs, 1)
    return {
        "gpu_seconds_per_doc": round(gpu_seconds / docs, 3),
        "cpu_core_seconds_per_doc": round(cpu_core_seconds / docs, 3),
    }


def price(gpu_seconds: float, cpu_core_seconds: float, gpu_hour: float, core_hour: float) -> float:
    return gpu_seconds / 3600.0 * gpu_hour + cpu_core_seconds / 3600.0 * core_hour


def sweep(gpu_seconds: float, cpu_core_seconds: float, docs: int,
          rates: dict | None = None) -> list[dict]:
    rates = rates or ILLUSTRATIVE_RATES
    rows: list[dict] = []
    cloud = rates["cloud"]
    rows.append({
        "scenario": f"cloud list (gpu ${cloud['gpu_hour']}/h, core ${cloud['core_hour']}/h)",
        "gpu_hour": cloud["gpu_hour"], "core_hour": cloud["core_hour"],
        "dollar_per_doc": round(price(gpu_seconds, cpu_core_seconds,
                                      cloud["gpu_hour"], cloud["core_hour"]) / max(docs, 1), 4),
    })
    owned = rates["owned"]
    for util in owned.get("utilisation", [0.5]):
        gpu_hour = owned["gpu_hour"] / util
        core_hour = owned["core_hour"] / util
        rows.append({
            "scenario": f"owned hardware @ {int(util * 100)}% utilisation",
            "gpu_hour": round(gpu_hour, 2), "core_hour": round(core_hour, 3),
            "dollar_per_doc": round(price(gpu_seconds, cpu_core_seconds, gpu_hour, core_hour)
                                    / max(docs, 1), 4),
        })
    return rows
