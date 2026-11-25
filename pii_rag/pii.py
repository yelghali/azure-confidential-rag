import base64
import hashlib
import hmac
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from azure.core.credentials import TokenCredential
from cryptography.fernet import Fernet

TOKEN = re.compile(r"\[PII_[A-Z_]+_[0-9a-f]{24}\]")
MAX_TEXT = 4000


class ProtectionError(Exception):
    """A message cannot safely cross the protection boundary."""


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    category: str


class Detector(Protocol):
    def detect(self, text: str) -> list[Span]: ...


def validate_text(text: str, *, allow_tokens: bool = False) -> None:
    if not text.strip() or len(text) > MAX_TEXT:
        raise ProtectionError(f"Text must contain 1-{MAX_TEXT} characters")
    if not allow_tokens and ("[PII_" in text or "[REDACTED_" in text):
        raise ProtectionError("Reserved token syntax is not accepted in source or question text")


def normalize_spans(text: str, spans: list[Span]) -> list[Span]:
    # Merge overlapping detections rather than accidentally leaving part of a value exposed.
    result: list[Span] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if not (0 <= span.start < span.end <= len(text)):
            raise ProtectionError("Detector returned an invalid span")
        category = re.sub("[^A-Z_]", "_", span.category.upper())[:40] or "ENTITY"
        current = Span(span.start, span.end, category)
        if result and current.start < result[-1].end:
            previous = result.pop()
            result.append(Span(previous.start, max(previous.end, current.end), previous.category))
        else:
            result.append(current)
    return result


class AzureDetector:
    def __init__(
        self,
        endpoint: str,
        credential: TokenCredential,
        client: httpx.Client,
        api_version: str = "2025-11-01",
    ):
        self.endpoint, self.credential, self.client = endpoint, credential, client
        self.api_version = api_version

    def detect(self, text: str) -> list[Span]:
        token = self.credential.get_token("https://cognitiveservices.azure.com/.default").token
        response = self.client.post(
            f"{self.endpoint}/language/:analyze-text",
            params={"api-version": self.api_version},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "kind": "PiiEntityRecognition",
                "parameters": {
                    "modelVersion": "latest",
                    "stringIndexType": "UnicodeCodePoint",
                    "loggingOptOut": True,
                },
                "analysisInput": {"documents": [{"id": "1", "language": "en", "text": text}]},
            },
        )
        response.raise_for_status()
        results = response.json()["results"]
        documents = results.get("documents", [])
        if results.get("errors") or len(documents) != 1 or documents[0].get("warnings"):
            raise ProtectionError("Azure PII did not return a complete warning-free analysis")
        return normalize_spans(
            text,
            [
                Span(e["offset"], e["offset"] + e["length"], e["category"])
                for e in documents[0]["entities"]
            ],
        )


class PresidioDetector:
    def __init__(self, endpoint: str, client: httpx.Client):
        self.endpoint, self.client = endpoint, client

    def detect(self, text: str) -> list[Span]:
        response = self.client.post(
            f"{self.endpoint}/analyze",
            json={"text": text, "language": "en", "score_threshold": 0.0},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, list):
            raise ProtectionError("Presidio returned an invalid analysis")
        return normalize_spans(text, [Span(e["start"], e["end"], e["entity_type"]) for e in result])


class SyntheticDetector:
    """Recognizes fixture identifiers only. Never a replacement for a real detector."""

    def detect(self, text: str) -> list[Span]:
        patterns = {
            "EMAIL": r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
            "PERSON": r"\b(?:Alice Martin|Bob Chen|Clara Jones)\b",
            "CREDIT_CARD": r"\b(?:\d[ -]?){15}\d\b",
            "ADDRESS": r"(?:123 Example Avenue|456 Sample Road|789 Demo Street)"
            r", [A-Za-z ]+, [A-Z]{2} \d{5}",
            "PHONE": r"\+1 \(202\) 555-01(?:43|68|99)",
            "GOVERNMENT_ID": r"\b123-45-6789\b",
            "IBAN": r"\bGB82 WEST 1234 5698 7654 32\b",
        }
        return normalize_spans(
            text,
            [
                Span(m.start(), m.end(), category)
                for category, pattern in patterns.items()
                for m in re.finditer(pattern, text)
            ],
        )


def redact_question(text: str, detector: Detector) -> str:
    validate_text(text)
    for span in reversed(normalize_spans(text, detector.detect(text))):
        text = text[: span.start] + f"[REDACTED_{span.category}]" + text[span.end :]
    return text


class Vault:
    """Single-replica sample store: original values encrypted; no clear-text lookup index."""

    def __init__(self, path: Path, key: bytes):
        self.cipher = Fernet(key)
        self.secret = base64.urlsafe_b64decode(key)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS mappings "
            "(collection TEXT, scope TEXT, token TEXT, value BLOB, "
            "PRIMARY KEY(collection, scope, token))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS documents "
            "(collection TEXT, document TEXT, scope TEXT, PRIMARY KEY(collection, document))"
        )
        self.db.commit()

    def protect(self, collection: str, scope: str, text: str, detector: Detector) -> str:
        validate_text(text)
        spans = normalize_spans(text, detector.detect(text))
        with self.lock, self.db:
            for span in reversed(spans):
                original = text[span.start : span.end]
                message = f"{collection}\x00{scope}\x00{span.category}\x00{original}".encode()
                digest = hmac.new(self.secret, message, hashlib.sha256).hexdigest()[:24]
                token = f"[PII_{span.category}_{digest}]"
                self.db.execute(
                    "INSERT OR IGNORE INTO mappings VALUES (?, ?, ?, ?)",
                    (collection, scope, token, self.cipher.encrypt(original.encode())),
                )
                text = text[: span.start] + token + text[span.end :]
            self.db.execute(
                "INSERT OR REPLACE INTO documents VALUES (?, ?, ?)",
                (collection, scope.split(":")[0], scope),
            )
        return text

    def restore(
        self,
        collection: str,
        text: str,
        evidence: dict[str, set[str]],
    ) -> tuple[str, int]:
        blocked = 0

        def substitute(match: re.Match[str]) -> str:
            nonlocal blocked
            token = match.group()
            scopes = evidence.get(token, set())
            with self.lock:
                rows = [
                    self.db.execute(
                        "SELECT m.value FROM mappings m JOIN documents d "
                        "ON m.collection=d.collection AND m.scope=d.scope "
                        "WHERE m.collection=? AND m.scope=? AND m.token=?",
                        (collection, scope, token),
                    ).fetchone()
                    for scope in scopes
                ]
            values = {self.cipher.decrypt(row[0]).decode() for row in rows if row}
            if len(values) != 1:
                blocked += 1
                return "[REDACTED]"
            return values.pop()

        return TOKEN.sub(substitute, text), blocked

    def close(self) -> None:
        self.db.close()
