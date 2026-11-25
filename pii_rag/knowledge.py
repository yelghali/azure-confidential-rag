import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx
from azure.core.credentials import TokenCredential

from pii_rag.config import collection_name
from pii_rag.pii import TOKEN, Detector, ProtectionError, Vault, validate_text


@dataclass(frozen=True)
class Evidence:
    id: str
    content: str
    scope: str
    collection: str

    def public(self) -> dict[str, str]:
        return asdict(self)


class Retriever(Protocol):
    def retrieve(self, collection: str, query: str) -> list[Evidence]: ...


def chunks(text: str, size: int = 1200) -> list[str]:
    result: list[str] = []
    while len(text) > size:
        end = text.rfind(" ", 0, size + 1)
        if end < size // 2:
            end = size
        # A long unbroken input may force a cut, but never inside a protected token.
        for token in TOKEN.finditer(text):
            if token.start() < end < token.end():
                end = token.end()
                break
        result.append(text[:end])
        text = text[end:].lstrip()
    if text:
        result.append(text)
    return result


def ingest(
    vault: Vault,
    detector: Detector,
    collection: str,
    document: str,
    text: str,
) -> list[Evidence]:
    validate_text(text)
    version = hashlib.sha256(text.encode()).hexdigest()
    scope = f"{document}:{version}"
    protected = vault.protect(collection, scope, text, detector)
    return [
        Evidence(f"{document}-{i}", content, scope, collection)
        for i, content in enumerate(chunks(protected))
    ]


class DemoKnowledge:
    def __init__(self) -> None:
        self.documents: dict[str, list[Evidence]] = {}

    def retrieve(self, collection: str, query: str) -> list[Evidence]:
        words = set(re.findall(r"[a-z]{3,}", query.lower())) - {
            "the",
            "and",
            "what",
            "are",
            "their",
            "how",
            "should",
            "were",
            "with",
            "for",
        }
        ranked = sorted(
            self.documents.get(collection, []),
            key=lambda doc: len(words & set(re.findall(r"[a-z]{3,}", doc.content.lower()))),
            reverse=True,
        )
        return [
            doc for doc in ranked if words & set(re.findall(r"[a-z]{3,}", doc.content.lower()))
        ][:3]


class SearchREST:
    def __init__(
        self,
        endpoint: str,
        credential: TokenCredential,
        client: httpx.Client,
        api_version: str,
    ):
        self.endpoint, self.credential = endpoint, credential
        self.client, self.api_version = client, api_version

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> dict:
        token = self.credential.get_token("https://search.azure.com/.default").token
        response = self.client.request(
            method,
            self.endpoint + path,
            params={"api-version": self.api_version},
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def retrieve(self, collection: str, query: str) -> list[Evidence]:
        source = collection_name(collection, "source")
        result = self.request(
            "POST",
            f"/knowledgebases('{collection_name(collection, 'kb')}')/retrieve",
            {
                "intents": [{"type": "semantic", "search": query}],
                "retrievalReasoningEffort": {"kind": "minimal"},
                "outputMode": "extractiveData",
                "maxRuntimeInSeconds": 30,
                "maxOutputDocuments": 5,
                "maxOutputSizeInTokens": 6000,
                "knowledgeSourceParams": [
                    {
                        "knowledgeSourceName": source,
                        "kind": "searchIndex",
                        "alwaysQuerySource": True,
                        "failOnError": True,
                        "includeReferences": True,
                        "includeReferenceSourceData": True,
                    }
                ],
            },
        )
        evidence = []
        for reference in result.get("references", []):
            data = reference.get("sourceData")
            if (
                not isinstance(data, dict)
                or data.get("collection") != collection
                or not all(isinstance(data.get(k), str) and data[k] for k in ("content", "scope"))
            ):
                raise ProtectionError("Foundry IQ returned missing or incorrectly scoped evidence")
            evidence.append(
                Evidence(
                    str(reference["id"]),
                    data["content"],
                    data["scope"],
                    collection,
                )
            )
        return evidence

    def run_indexer(self, collection: str) -> str:
        indexer = collection_name(collection, "indexer")
        try:
            self.request("POST", f"/indexers/{indexer}/run")
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 409:
                raise
            status = self.request("GET", f"/indexers/{indexer}/status")
            if (status.get("lastResult") or {}).get("status") != "inProgress":
                raise
            return "uploaded; indexer busy, awaiting next scheduled run"
        return "queued"


def evidence_tokens(evidence: list[Evidence], collection: str) -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for doc in evidence:
        if doc.collection != collection:
            raise ProtectionError("Cross-collection evidence was rejected")
        for token in TOKEN.findall(doc.content):
            allowed.setdefault(token, set()).add(doc.scope)
    return allowed
