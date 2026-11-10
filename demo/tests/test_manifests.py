from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_MANIFESTS = REPO_ROOT / "manifests" / "base"

# Kubernetes port names are IANA_SVC_NAMEs: at most 15 characters, lowercase
# alphanumeric or '-', and alphanumeric at both ends. The API server rejects
# anything longer, which is easy to miss because `kustomize build` accepts it.
PORT_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,13}[a-z0-9])?$")


def _documents():
    for path in sorted(BASE_MANIFESTS.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict):
                yield path, doc


def _pod_spec(doc: dict) -> dict:
    return doc.get("spec", {}).get("template", {}).get("spec", {})


def test_container_and_service_port_names_are_valid():
    failures = []
    for path, doc in _documents():
        kind = doc.get("kind")
        if kind == "Deployment":
            for container in _pod_spec(doc).get("containers", []):
                for port in container.get("ports", []):
                    name = port.get("name")
                    if name and not PORT_NAME.match(name):
                        failures.append(
                            f"{path.name}: container {container.get('name')} "
                            f"port name {name!r}"
                        )
        elif kind == "Service":
            for port in doc.get("spec", {}).get("ports", []):
                name = port.get("name")
                if name and not PORT_NAME.match(name):
                    failures.append(
                        f"{path.name}: service {doc['metadata']['name']} "
                        f"port name {name!r}"
                    )
    assert not failures, "invalid Kubernetes port names:\n" + "\n".join(failures)
