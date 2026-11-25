import hashlib
import logging
import os
import re
import secrets
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import httpx
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field

from pii_rag.agent import Agent, DemoAgent, FoundryAgent
from pii_rag.config import COLLECTIONS, Settings, collection_name, required
from pii_rag.knowledge import DemoKnowledge, SearchREST, evidence_tokens, ingest
from pii_rag.pii import (
    TOKEN,
    AzureDetector,
    Detector,
    PresidioDetector,
    ProtectionError,
    SyntheticDetector,
    Vault,
    normalize_spans,
    redact_question,
    validate_text,
)
from pii_rag.samples import PRIVACY_QUESTIONS, SAMPLES, SUGGESTIONS

logger = logging.getLogger("pii_rag")
STATIC = Path(__file__).parent / "static"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentInput(Input):
    text: str = Field(min_length=1, max_length=4000)


class SessionInput(Input):
    collection: str


class ChatInput(Input):
    session_id: str = Field(min_length=20, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    restore: bool = False


class DisplayInput(Input):
    session_id: str = Field(min_length=20, max_length=100)
    answer_id: str = Field(min_length=20, max_length=100)
    restore: bool


class SkillData(Input):
    text: str = Field(min_length=1, max_length=4000)
    document_id: str = Field(min_length=1, max_length=2048)


class SkillRecord(Input):
    recordId: str = Field(min_length=1, max_length=100)
    data: SkillData


class SkillInput(Input):
    values: list[SkillRecord] = Field(min_length=1, max_length=4)


@dataclass
class SavedAnswer:
    id: str
    text: str
    allowed: dict[str, set[str]]
    evidence_tokens: int
    scan_counts: dict[str, int]
    blocked: int
    model_masks: int
    question_masks: int


@dataclass
class Session:
    collection: str
    conversation: str
    created: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    failed: bool = False
    latest: SavedAnswer | None = None


def safe_output(text: str, detector: Detector) -> tuple[str, dict[str, int]]:
    if not text.strip() or len(text) > 4000:
        raise ProtectionError("Answer exceeded the safe scan limit; ask for a shorter answer")
    masked = TOKEN.sub(lambda match: " " * len(match.group()), text)
    spans = normalize_spans(masked, detector.detect(masked)) if masked.strip() else []
    for span in reversed(spans):
        text = text[: span.start] + "[REDACTED]" + text[span.end :]
    return text, dict(Counter(span.category for span in spans))


def display_answer(vault: Vault, session: Session, saved: SavedAnswer, restore: bool) -> dict:
    answer = saved.text
    occurrences = len(TOKEN.findall(saved.text))
    missing = 0
    if restore:
        answer, missing = vault.restore(session.collection, saved.text, saved.allowed)
    blocked = saved.blocked + missing
    return {
        "answer_id": saved.id,
        "answer": answer,
        "sanitized_answer": saved.text,
        "restored": restore,
        "blocked_tokens": blocked,
        "warning": "Some token references could not be restored." if blocked else None,
        "privacy": {
            "answer_placeholders": occurrences,
            "evidence_placeholders": saved.evidence_tokens,
            "restored_occurrences": occurrences - missing if restore else 0,
            "blocked_occurrences": blocked,
            "output_scan_redactions": sum(saved.scan_counts.values()),
            "output_scan_categories": saved.scan_counts,
            "model_redaction_markers": saved.model_masks,
            "question_redactions": saved.question_masks,
        },
    }


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        key = settings.mapping_key.encode()
        if not key:
            path = settings.data_dir / "demo.key"
            if not path.exists():
                # Local fixture key only; Azure mode requires explicit secret configuration.
                path.write_bytes(Fernet.generate_key())
            key = path.read_bytes()
        self.vault = Vault(settings.data_dir / "mappings.sqlite3", key)
        self.http = httpx.Client(timeout=60, follow_redirects=False)
        self.sessions: dict[str, Session] = {}
        self.session_lock = threading.Lock()
        self.credential: DefaultAzureCredential | None = None
        self.project: AIProjectClient | None = None
        self.blobs: BlobServiceClient | None = None
        self.search: SearchREST | None = None
        self.demo: DemoKnowledge | None = None
        self.detector: Detector
        self.agent: Agent
        if settings.mode == "demo":
            self.detector = SyntheticDetector()
            self.demo = DemoKnowledge()
            for collection, samples in SAMPLES.items():
                self.demo.documents[collection] = [
                    doc
                    for source, text in samples
                    for doc in ingest(self.vault, self.detector, collection, source, text)
                ]
            self.agent = DemoAgent(self.demo)
        else:
            self.credential = DefaultAzureCredential()
            self.detector = (
                AzureDetector(
                    required("RAG_LANGUAGE_ENDPOINT"),
                    self.credential,
                    self.http,
                    os.getenv("RAG_LANGUAGE_API_VERSION", "2025-11-01"),
                )
                if settings.provider == "azure"
                else PresidioDetector(required("RAG_PRESIDIO_ENDPOINT"), self.http)
            )
            self.search = SearchREST(
                required("RAG_SEARCH_ENDPOINT"),
                self.credential,
                self.http,
                os.getenv("RAG_SEARCH_API_VERSION", "2026-10-01"),
            )
            self.project = AIProjectClient(
                endpoint=required("RAG_PROJECT_ENDPOINT"),
                credential=self.credential,
            )
            self.agent = FoundryAgent(self.project, self.search, self.detector)
            self.blobs = BlobServiceClient(
                account_url=required("RAG_BLOB_ACCOUNT_URL"),
                credential=self.credential,
            )

    def close(self) -> None:
        self.http.close()
        self.vault.close()
        if self.project:
            self.project.close()
        if self.blobs:
            self.blobs.close()
        if self.credential:
            self.credential.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = Runtime(settings or Settings.load())
        app.state.runtime = runtime
        yield
        runtime.close()

    app = FastAPI(title="PII-protected RAG", lifespan=lifespan, docs_url=None, redoc_url=None)

    def runtime() -> Runtime:
        return app.state.runtime

    def authorize(
        x_app_key: Annotated[str, Header()] = "",
    ) -> Runtime:
        rt = runtime()
        if rt.settings.mode != "demo" and not secrets.compare_digest(
            x_app_key,
            rt.settings.app_key,
        ):
            raise HTTPException(401, "Application key required")
        return rt

    def collection_ok(collection: str) -> None:
        if collection not in COLLECTIONS:
            raise HTTPException(404, "Unknown collection")

    @app.middleware("http")
    async def headers(request, call_next):
        length = request.headers.get("content-length", "0")
        if not length.isdecimal():
            return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        if int(length) > 100_000:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(ProtectionError)
    async def protection_error(request, error: ProtectionError):
        logger.warning("protection_request_rejected")
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"detail": "Invalid request fields or text limits"}, status_code=422)

    async def upstream_error(request, error):
        logger.warning("upstream_request_failed type=%s", type(error).__name__)
        return JSONResponse(
            {
                "detail": "Azure/provider request failed. Check service configuration and roles; "
                "start a new chat after a failed turn."
            },
            status_code=502,
        )

    for error_type in (httpx.HTTPError, AzureError, OpenAIError):
        app.add_exception_handler(error_type, upstream_error)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "running", "mode": runtime().settings.mode}

    @app.get("/api/config")
    def configuration(rt: Annotated[Runtime, Depends(authorize)]):
        return {
            "mode": rt.settings.mode,
            "provider": "synthetic fixture only" if rt.demo else rt.settings.provider,
            "collections": [{"id": key, "label": value} for key, value in COLLECTIONS.items()],
            "suggestions": SUGGESTIONS,
            "privacy_questions": PRIVACY_QUESTIONS,
            "samples": SAMPLES if rt.demo else None,
            "privacy_examples": {
                collection: [source for source, _ in samples]
                for collection, samples in SAMPLES.items()
            },
            "max_document_chars": 4000,
            "notice": (
                "Synthetic local demo: lexical excerpts, no LLM or Azure connection. "
                "Only fixture identifiers are detected. Upload is disabled."
                if rt.demo
                else "Live Azure mode. Incidental PII only; English text, 4,000 characters/document. "
                "No document-level RBAC. Use a uniformly shareable corpus."
            ),
        }

    @app.get("/api/examples/{collection}/{sample_id}")
    def example(collection: str, sample_id: str, rt: Annotated[Runtime, Depends(authorize)]):
        collection_ok(collection)
        original = dict(SAMPLES[collection]).get(sample_id)
        if original is None:
            raise HTTPException(404, "Unknown synthetic sample")
        # Blob text extraction appends LF; local fixtures retain the original text.
        versions = {
            hashlib.sha256(text.encode()).hexdigest() for text in (original, original + "\n")
        }
        if rt.demo:
            candidates = [d.public() for d in rt.demo.documents[collection]]
        else:
            assert rt.search
            result = rt.search.request(
                "POST",
                f"/indexes/{collection_name(collection, 'index')}/docs/search",
                {
                    "search": f'"{sample_id}"',
                    "searchFields": "content",
                    "top": 100,
                    "select": "content,scope,collection",
                },
            )
            candidates = result.get("value", [])
        matches = [
            d
            for d in candidates
            if d.get("collection") == collection
            and d.get("scope", "").rsplit(":", 1)[-1] in versions
        ]
        if not matches:
            raise HTTPException(
                404, "Current sample is not indexed. Run scripts.check_azure --seed."
            )
        scope = matches[0]["scope"]
        chunks = [d["content"] for d in matches if d["scope"] == scope]
        counts = Counter(
            token[5:].rsplit("_", 1)[0] for content in chunks for token in TOKEN.findall(content)
        )
        return {
            "sample_id": sample_id,
            "collection": collection,
            "synthetic": True,
            "original": original,
            "indexed_chunks": chunks,
            "pii_counts": dict(counts),
            "source": "local fixture, not Azure" if rt.demo else "Azure AI Search",
            "notice": "Original preview is a bundled fictional sample only. "
            "Protected chunks are read from the selected collection's actual index.",
        }

    @app.post("/api/sessions")
    def new_session(data: SessionInput, rt: Annotated[Runtime, Depends(authorize)]):
        collection_ok(data.collection)
        with rt.session_lock:
            if len(rt.sessions) >= 100:
                raise HTTPException(429, "Session limit reached. Close unused chats or restart.")
            session_id = secrets.token_urlsafe(32)
            conversation = rt.agent.create_conversation(data.collection)
            rt.sessions[session_id] = Session(data.collection, conversation)
        return {"session_id": session_id, "collection": data.collection}

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str, rt: Annotated[Runtime, Depends(authorize)]):
        with rt.session_lock:
            session = rt.sessions.get(session_id)
        if session:
            with session.lock:
                rt.agent.delete_conversation(session.collection, session.conversation)
                with rt.session_lock:
                    rt.sessions.pop(session_id, None)
        return Response(status_code=204)

    @app.post("/api/chat")
    def chat(data: ChatInput, rt: Annotated[Runtime, Depends(authorize)]):
        with rt.session_lock:
            session = rt.sessions.get(data.session_id)
        if not session or time.monotonic() - session.created > 3600:
            raise HTTPException(404, "Chat expired or unknown. Start a new chat.")
        if not session.lock.acquire(blocking=False):
            raise HTTPException(409, "Wait for the current turn to finish")
        try:
            if session.failed:
                raise HTTPException(409, "Previous turn failed. Start a new chat.")
            question = redact_question(data.question, rt.detector)
            session.failed = True
            result = rt.agent.answer(session.collection, session.conversation, question)
            allowed = evidence_tokens(result.evidence, session.collection)
            answer, scan_counts = safe_output(result.answer, rt.detector)
            # Mask unsupported token references even when restoration is switched off.
            blocked = len([t for t in TOKEN.findall(answer) if t not in allowed])
            answer = TOKEN.sub(
                lambda m: m.group() if m.group() in allowed else "[REDACTED]",
                answer,
            )
            saved = SavedAnswer(
                id=secrets.token_urlsafe(24),
                text=answer,
                allowed=allowed,
                evidence_tokens=sum(len(TOKEN.findall(d.content)) for d in result.evidence),
                scan_counts=scan_counts,
                blocked=blocked,
                model_masks=len(re.findall(r"\[REDACTED(?:_[A-Z_]+)?\]", result.answer)),
                question_masks=len(re.findall(r"\[REDACTED_[A-Z_]+\]", question)),
            )
            session.latest = saved
            displayed = display_answer(rt.vault, session, saved, data.restore)
            session.failed = False
            return {
                **displayed,
                "sanitized_question": question,
                "evidence": [d.public() for d in result.evidence],
                "mode": rt.settings.mode,
            }
        finally:
            session.lock.release()

    @app.post("/api/answer/display")
    def redisplay(data: DisplayInput, rt: Annotated[Runtime, Depends(authorize)]):
        with rt.session_lock:
            session = rt.sessions.get(data.session_id)
        if not session or time.monotonic() - session.created > 3600:
            raise HTTPException(404, "Chat expired or unknown. Start a new chat.")
        with session.lock:
            saved = session.latest
            if not saved or saved.id != data.answer_id:
                raise HTTPException(404, "This is not the latest answer in this conversation.")
            return display_answer(rt.vault, session, saved, data.restore)

    @app.post("/api/collections/{collection}/documents", status_code=202)
    def upload(collection: str, data: DocumentInput, rt: Annotated[Runtime, Depends(authorize)]):
        collection_ok(collection)
        if not rt.blobs or not rt.search:
            raise HTTPException(400, "Upload is disabled in the synthetic-only demo")
        # Reject reserved syntax before the raw source is submitted to the managed indexer.
        validate_text(data.text)
        blob_id = secrets.token_hex(16) + ".txt"
        rt.blobs.get_blob_client(
            container=collection_name(collection, "raw"),
            blob=blob_id,
        ).upload_blob(
            data.text.encode(), content_settings=ContentSettings(content_type="text/plain")
        )
        try:
            status = rt.search.run_indexer(collection)
        except httpx.HTTPError as error:
            raise ProtectionError(
                "Document was uploaded but the indexer could not be started. "
                "Check ingestion status before uploading it again."
            ) from error
        return {"status": status, "document_id": blob_id, "provider": rt.settings.provider}

    @app.get("/api/collections/{collection}/status")
    def status(collection: str, rt: Annotated[Runtime, Depends(authorize)]):
        collection_ok(collection)
        if rt.demo:
            return {"status": "ready", "documents": len(rt.demo.documents[collection])}
        assert rt.search
        result = rt.search.request(
            "GET", f"/indexers/{collection_name(collection, 'indexer')}/status"
        )
        last = result.get("lastResult") or {}
        return {
            "status": last.get("status", result.get("status")),
            "processed": last.get("itemsProcessed", 0),
            "failed": last.get("itemsFailed", 0),
            # Indexer errors can include document fragments; never echo their payloads.
            "errors": len(last.get("errors", [])),
            "warnings": len(last.get("warnings", [])),
            "notice": "Inspect restricted Azure diagnostics if errors/warnings are nonzero.",
        }

    @app.post("/api/skill/{collection}")
    def skill(
        collection: str,
        data: SkillInput,
        x_skill_key: Annotated[str, Header()] = "",
    ):
        rt = runtime()
        collection_ok(collection)
        if not rt.settings.skill_key or not secrets.compare_digest(
            x_skill_key,
            rt.settings.skill_key,
        ):
            raise HTTPException(401, "Skill key required")
        values = []
        for record in data.values:
            try:
                document = hashlib.sha256(record.data.document_id.encode()).hexdigest()
                docs = ingest(
                    rt.vault,
                    rt.detector,
                    collection,
                    document,
                    record.data.text,
                )
                values.append(
                    {
                        "recordId": record.recordId,
                        "data": {
                            "chunks": [
                                {"text": d.content, "scope": d.scope, "collection": d.collection}
                                for d in docs
                            ]
                        },
                        "errors": [],
                        "warnings": [],
                    }
                )
            except (ProtectionError, httpx.HTTPError, AzureError) as error:
                logger.warning("skill_record_quarantined type=%s", type(error).__name__)
                values.append(
                    {
                        "recordId": record.recordId,
                        "data": {},
                        "errors": [
                            {"message": "PII processing failed; no sanitized content published."}
                        ],
                        "warnings": [],
                    }
                )
        return {"values": values}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
