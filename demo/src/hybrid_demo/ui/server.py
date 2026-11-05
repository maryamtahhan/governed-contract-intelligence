"""Serve the stage UI: replay a scored run, optionally run one document live.

Replay is the default and needs no cluster: it renders the records from a
finished run, so the demo still works if the endpoint is down. Live mode adds
one button per document that sends that document through the same pipeline
code the batch runner uses, directly to an OpenAI-compatible endpoint.

    uv run hybrid-ui --run ../runs/cuad-test/arm-direct-moe-gpu-v2 \
        --data data/cuad-test
    uv run hybrid-ui --run ... --data ... --endpoint http://127.0.0.1:8000/v1

The endpoint is required for live mode because a browser cannot call vLLM
directly across origins; this small proxy is what keeps that off the page.
Because those POST routes are a proxy to the endpoint, live mode can be
gated with ``--live-token`` (or ``HYBRID_UI_LIVE_TOKEN``), and a non-loopback
``--host`` with live mode refuses to start without it.

The same server serves the presenter's guide at /guide when a guide file is
present, so the walkthrough and the page it describes share one origin.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from ..configs import catalog
from ..pipeline.direct import run_doc, run_doc_stream
from .corpus import CorpusError, decode_archive, documents_from_archive, documents_from_files
from .payload import build_document, build_payload
from .telemetry import LivePools, run_artifacts
from .telemetry import metric as _metric

# A pasted or uploaded contract is plain text at this point: the ingestion
# step that turns PDF/DOCX into text (docling) is designed, not built, so the
# live path accepts text and says so instead of pretending to parse a PDF.
MAX_LIVE_CHARS = 400_000

# Submitted documents stay in memory for the life of the process, so the
# store is bounded both ways: at most this many documents and this many
# characters, evicting the oldest first.
MAX_LIVE_DOCS = 128
MAX_LIVE_TOTAL_CHARS = 16_000_000


def _live_id(name: str | None, seq: int) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "pasted")).strip("-.")[:48]
    return f"live-{slug or 'pasted'}-{seq}"


class LiveDocs:
    """Submitted documents, kept in memory and bounded.

    The page keeps every result it was sent, so the server's copy exists only
    to re-run a document. Both bounds evict oldest-first and nothing is ever
    written to disk; ids come from a monotonic sequence, so eviction cannot
    reuse an id the page still shows.
    """

    def __init__(self, max_docs: int = MAX_LIVE_DOCS,
                 max_chars: int = MAX_LIVE_TOTAL_CHARS) -> None:
        self.max_docs = max_docs
        self.max_chars = max_chars
        self._docs: dict[str, str] = {}
        self._chars = 0
        self._seq = 0

    def add(self, name: str | None, text: str) -> str:
        """Store one submitted document under a fresh id, evicting if needed."""
        self._seq += 1
        doc_id = _live_id(name, self._seq)
        self._docs[doc_id] = text
        self._chars += len(text)
        while self._docs and (len(self._docs) > self.max_docs
                              or self._chars > self.max_chars):
            # Insertion order is oldest-first, so the first key is the FIFO victim.
            oldest = next(iter(self._docs))
            self._chars -= len(self._docs.pop(oldest))
        return doc_id

    def __len__(self) -> int:
        return len(self._docs)

    def __contains__(self, doc_id: object) -> bool:
        return doc_id in self._docs


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _default_guide() -> Path | None:
    """The presenter's guide that ships with this checkout, if present.

    ``parents[4]`` is the repository root when the demo runs from source
    (ui -> hybrid_demo -> src -> demo -> repo). A non-editable install has no
    repository around it, so /guide then asks for --guide explicitly.
    """
    candidates = (
        Path(__file__).with_name("walkthrough.html"),
        Path(__file__).resolve().parents[4] / "docs" / "stage-review-walkthrough.html",
    )
    return next((p for p in candidates if p.exists()), None)


def _loopback(host: str) -> bool:
    """Is this bind address loopback? An unresolvable name counts as not."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _live_bind_error(host: str, endpoint: str | None, token: str | None,
                     allow_insecure: bool) -> str | None:
    """Why this bind would expose an unauthenticated OpenAI proxy, if it would.

    Live mode forwards request bodies to ``--endpoint``. On a non-loopback
    bind without a token, anyone who can reach the port can drive it, so the
    start is refused unless a token is set or the lab explicitly opts in.
    """
    if endpoint and not _loopback(host) and not (token or allow_insecure):
        return (f"--host {host} is not loopback and live mode is on; pass "
                "--live-token, or --allow-insecure-live for lab use")
    return None


def create_app(run_dir: Path, data_dir: Path, endpoint: str | None,
               guide_path: Path | None = None,
               compare_dir: Path | None = None,
               pools: list[tuple[str, str]] | None = None,
               runs_dir: Path | None = None,
               live_token: str | None = None) -> FastAPI:
    payload = build_payload(run_dir, data_dir, live_enabled=bool(endpoint))
    # A second run of the same documents, for an A/B the page can switch to.
    # It is built once, like the primary payload, so a toggle costs no I/O.
    compare_payload = (build_payload(compare_dir, data_dir,
                                     live_enabled=bool(endpoint))
                       if compare_dir else None)
    truth_docs = {d["doc_id"]: d for d in
                  json.loads((data_dir / "ground_truth.json").read_text())["documents"]}
    known = {d["doc_id"]: d for d in payload["documents"]}
    clause_keys = payload["clause_keys"]
    page = (Path(__file__).with_name("index.html")).read_text()
    telemetry_page = (Path(__file__).with_name("monitor.html")).read_text()
    guide = guide_path.read_text() if guide_path and guide_path.exists() else None
    # No --pool flags but a live endpoint: offer that one endpoint's metrics,
    # named for what it is, rather than showing an empty telemetry page.
    pools = list(pools or [])
    if not pools and endpoint:
        pools = [("endpoint", endpoint)]
    live_pools = LivePools(pools) if pools else None
    focus = [run_dir] + ([compare_dir] if compare_dir else [])
    runs_dir = runs_dir or run_dir.parent

    app = FastAPI(title="Stage UI")
    # Live runs are interactive and one document at a time; the semaphore is
    # wide enough to let classify and the extracts fan out, no wider.
    sem = asyncio.Semaphore(max(8, len(clause_keys) + 1))
    # Submitted documents live for the life of the process: the page keeps
    # their results, the server keeps only the text, bounded oldest-first.
    live_docs = LiveDocs()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/guide", response_class=HTMLResponse)
    def presenter_guide() -> str:
        if guide is None:
            raise HTTPException(404, "no presenter's guide configured; "
                                     "start with --guide <path>")
        return guide

    @app.get("/api/run")
    def run_data(arm: str | None = None) -> dict:
        if arm == "compare":
            if compare_payload is None:
                raise HTTPException(404, "server started without --compare")
            return compare_payload
        return {**payload, "compare_available": compare_payload is not None}

    @app.get("/api/configs")
    def config_catalog() -> dict:
        """The canonical routing-policy records, for pages that need the list."""
        return {"configs": catalog()}

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor_page() -> str:
        return telemetry_page

    # Sweep files appear while the server runs (a sweep finishing on the SNO),
    # so the artifact scan is re-read, but not on every eight-second poll.
    artifact_cache: dict = {"at": 0.0, "value": None}

    @app.get("/api/telemetry")
    async def telemetry() -> dict:
        """Recorded artifacts plus one live sample of every configured pool.

        The page polls this; each poll is one sample, so the interval rates are
        computed between polls here rather than in the browser.
        """
        if time.monotonic() - artifact_cache["at"] > 15:
            artifact_cache["value"] = run_artifacts(runs_dir, focus)
            artifact_cache["at"] = time.monotonic()
        return {
            "artifacts": artifact_cache["value"],
            "live": {
                "enabled": live_pools is not None,
                "pools": (await live_pools.sample()) if live_pools else [],
                "configured": [{"name": n, "url": u} for n, u in pools],
                "note": "A pool name is a routing tier, not a hardware claim; "
                        "the deployment's upstream config binds a tier to a process.",
            },
        }

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "run": payload["meta"]["run"],
                "live_enabled": bool(endpoint), "endpoint": endpoint,
                "guide": guide is not None, "pools": [n for n, _ in pools],
                "monitor": True}

    def _resolve(body: dict) -> tuple[str, str, dict, bool]:
        """Either a document from the run (doc_id) or one submitted now (text).

        A submitted document has no annotations, so it is rendered unscored.
        """
        submitted = body.get("text")
        if submitted is not None:
            text = str(submitted)
            if not text.strip():
                raise HTTPException(400, "the submitted document is empty")
            if len(text) > MAX_LIVE_CHARS:
                raise HTTPException(413, f"document is {len(text):,} characters; "
                                         f"the live path accepts up to {MAX_LIVE_CHARS:,}")
            doc_id = live_docs.add(body.get("name"), text)
            return doc_id, text, {}, False
        doc_id = body.get("doc_id")
        if doc_id not in known:
            raise HTTPException(404, f"{doc_id!r} is not part of this run")
        text_path = data_dir / "documents" / f"{doc_id}.txt"
        if not text_path.exists():
            raise HTTPException(404, f"source text for {doc_id} is not in {data_dir}")
        return doc_id, text_path.read_text(), truth_docs.get(doc_id, {}), True

    def _require_endpoint() -> str:
        if not endpoint:
            raise HTTPException(400, "server started without --endpoint; start it again "
                                     "with the vLLM base URL to enable live runs")
        return endpoint

    def _check_live_token(x_live_token: str | None = Header(None),
                          authorization: str | None = Header(None)) -> None:
        """The POST routes below are a proxy to --endpoint; gate them.

        Without this, a server bound beyond loopback is an open relay to
        whatever model it was pointed at. The comparison is constant-time so
        the refusal does not leak how much of the token matched.
        """
        if live_token is None:
            return
        sent = x_live_token
        if sent is None and authorization:
            scheme, _, value = authorization.partition(" ")
            sent = value if scheme.lower() == "bearer" else None
        if sent is None or not secrets.compare_digest(sent.encode(),
                                                      live_token.encode()):
            raise HTTPException(401, "this server was started with --live-token; "
                                     "send it as X-Live-Token or Authorization: Bearer")

    @app.post("/api/live")
    async def live(body: dict, _: None = Depends(_check_live_token)) -> dict:
        _require_endpoint()
        doc_id, text, truth, scored = _resolve(body)
        try:
            async with httpx.AsyncClient(base_url=endpoint, timeout=900.0) as client:
                records = await run_doc(client, sem, doc_id, text, clause_keys,
                                        parallel=True)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"endpoint {endpoint} failed: {e}") from e
        return {"doc_id": doc_id,
                "document": build_document(doc_id, text, records, truth,
                                           clause_keys, scored=scored),
                "records": records}

    @app.post("/api/live/stream")
    async def live_stream(body: dict,
                          _: None = Depends(_check_live_token)) -> StreamingResponse:
        """The same run as /api/live, one SSE event per finished stage.

        The page uses this so the strip lights as each call actually returns:
        one classify event, then the extracts in the order they finish, then
        risk, then the finished document. Same records, same scorer.
        """
        _require_endpoint()
        doc_id, text, truth, scored = _resolve(body)

        async def events():
            records = []
            try:
                async with httpx.AsyncClient(base_url=endpoint, timeout=900.0) as client:
                    async for rec in run_doc_stream(client, sem, doc_id, text,
                                                    clause_keys):
                        records.append(rec)
                        yield _sse({"type": "stage", "record": rec})
            except httpx.HTTPError as e:
                yield _sse({"type": "error",
                            "detail": f"endpoint {endpoint} failed: {e}"})
                return
            yield _sse({"type": "done", "doc_id": doc_id,
                        "document": build_document(doc_id, text, records, truth,
                                                   clause_keys, scored=scored)})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    def _corpus_docs(body: dict) -> tuple[list[dict], list[str]]:
        """Loose text files, or one archive, turned into documents.

        Both paths end at the same caps; the archive path never writes to
        disk, so a hostile member can at worst be read into a capped string.
        """
        try:
            if isinstance(body.get("files"), list) and body["files"]:
                return documents_from_files(body["files"])
            archive = body.get("archive") or {}
            if archive:
                # decode_archive refuses the payload's own size before the
                # corpus caps ever see a member of it.
                return documents_from_archive(str(archive.get("name") or "corpus"),
                                              decode_archive(str(archive.get("data") or "")))
        except CorpusError as e:
            # The code travels on the error (413 = too big, 400 = not
            # loadable); it must not be inferred from the message text.
            raise HTTPException(e.status_code, str(e)) from e
        raise HTTPException(400, "send `files` or `archive`")

    @app.post("/api/corpus/stream")
    async def corpus_stream(body: dict,
                            _: None = Depends(_check_live_token)) -> StreamingResponse:
        """A batch of submitted documents, streamed document by document.

        Two documents are in flight at once and all of them share the one
        request semaphore, so a corpus run is the single-document run repeated
        without either serialising it or opening the endpoint wide.
        """
        _require_endpoint()
        docs, skipped = _corpus_docs(body)
        batch = []
        for doc in docs:
            batch.append((live_docs.add(doc["name"], doc["text"]), doc))

        async def events():
            yield _sse({"type": "corpus_start",
                        "docs": [{"doc_id": d, "name": x["name"]}
                                 for d, x in batch],
                        "skipped": skipped})
            queue: asyncio.Queue = asyncio.Queue()
            per_batch = asyncio.Semaphore(2)

            async def one(client: httpx.AsyncClient, doc_id: str, doc: dict) -> None:
                async with per_batch:
                    await queue.put({"type": "doc_start", "doc_id": doc_id,
                                     "name": doc["name"]})
                    records = []
                    try:
                        async for rec in run_doc_stream(client, sem, doc_id,
                                                        doc["text"], clause_keys):
                            records.append(rec)
                            await queue.put({"type": "stage", "doc_id": doc_id,
                                             "record": rec})
                    except httpx.HTTPError as e:
                        await queue.put({"type": "doc_error", "doc_id": doc_id,
                                         "detail": f"endpoint {endpoint} failed: {e}"})
                        return
                    except Exception as e:  # one bad document must not sink the batch
                        await queue.put({"type": "doc_error", "doc_id": doc_id,
                                         "detail": f"{e.__class__.__name__}: {e}"})
                        return
                await queue.put({"type": "doc_done", "doc_id": doc_id,
                                 "document": build_document(
                                     doc_id, doc["text"], records, {},
                                     clause_keys, scored=False)})

            async def produce() -> None:
                try:
                    async with httpx.AsyncClient(base_url=endpoint,
                                                 timeout=900.0) as client:
                        await asyncio.gather(*[
                            one(client, doc_id, doc) for doc_id, doc in batch])
                finally:
                    await queue.put(None)

            task = asyncio.create_task(produce())
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield _sse(event)
            finally:
                await task
            yield _sse({"type": "done", "doc_ids": [d for d, _ in batch]})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/api/endpoint")
    async def endpoint_status() -> dict:
        """Is the live endpoint up and warm? Answers the header chip only."""
        if not endpoint:
            return {"live_enabled": False}
        base = endpoint.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        out: dict = {"live_enabled": True}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                models = (await client.get(f"{base}/v1/models")).json()
                out["model"] = (models.get("data") or [{}])[0].get("id")
                try:
                    text = (await client.get(f"{base}/metrics")).text
                    out["running"] = _metric(text, "vllm:num_requests_running")
                    out["waiting"] = _metric(text, "vllm:num_requests_waiting")
                except (httpx.HTTPError, ValueError):
                    pass
        except (httpx.HTTPError, ValueError) as e:
            out["error"] = e.__class__.__name__
        return out

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="stage UI for a scored run")
    parser.add_argument("--run", required=True, help="run directory with results.json")
    parser.add_argument("--data", default="data/cuad-test")
    parser.add_argument("--endpoint", default=None,
                        help="OpenAI-compatible base URL; enables live one-document runs")
    parser.add_argument("--live-token", default=os.environ.get("HYBRID_UI_LIVE_TOKEN"),
                        help="require this token on the live/corpus POST routes, "
                             "as X-Live-Token or Authorization: Bearer "
                             "(default: HYBRID_UI_LIVE_TOKEN)")
    parser.add_argument("--allow-insecure-live", action="store_true",
                        help="let live mode bind a non-loopback --host without "
                             "--live-token; lab use only — anyone who can reach "
                             "the port can then drive the endpoint")
    parser.add_argument("--guide", default=None,
                        help="HTML presenter's guide served at /guide "
                             "(default: docs/stage-review-walkthrough.html)")
    parser.add_argument("--compare", default=None,
                        help="a second run directory of the same documents; the "
                             "page offers an A/B switch between the two")
    parser.add_argument("--pool", action="append", default=[], metavar="NAME=URL",
                        help="a vLLM pool to poll for /api/telemetry, e.g. "
                             "cpu=http://vllm-cpu:8000; repeat for each tier")
    parser.add_argument("--runs-dir", default=None,
                        help="directory whose runs the telemetry page lists "
                             "(default: the run's parent directory)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default 127.0.0.1); with --endpoint, "
                             "a non-loopback host also needs --live-token or "
                             "--allow-insecure-live")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    pools: list[tuple[str, str]] = []
    for spec in args.pool:
        name, _, url = spec.partition("=")
        if not name or not url:
            parser.error(f"--pool wants NAME=URL, got {spec!r}")
        pools.append((name, url))

    refusal = _live_bind_error(args.host, args.endpoint, args.live_token,
                               args.allow_insecure_live)
    if refusal:
        parser.error(refusal)
    if args.endpoint and not args.live_token:
        # The proxy routes answer anyone who can reach the port. Bound to
        # loopback that is the operator's own machine; bound wider it is not.
        print(f"warning: live mode is unauthenticated — anyone who can reach "
              f"{args.host}:{args.port} can send requests to {args.endpoint} "
              "(fine on localhost; pass --live-token otherwise)", file=sys.stderr)

    guide = Path(args.guide) if args.guide else _default_guide()
    compare = Path(args.compare) if args.compare else None
    runs_dir = Path(args.runs_dir) if args.runs_dir else None
    app = create_app(Path(args.run), Path(args.data), args.endpoint, guide, compare,
                     pools=pools, runs_dir=runs_dir, live_token=args.live_token)
    print(f"run:  {args.run}")
    print(f"live: {args.endpoint or 'off (replay only)'}")
    print(f"pools: {', '.join(n for n, _ in pools) or 'off'}")
    print(f"compare: {compare or 'off'}")
    print(f"guide: {guide or 'off'}")
    print(f"telemetry: http://{args.host}:{args.port}/monitor")
    print(f"open: http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
