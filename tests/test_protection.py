import json

import httpx
import pytest
from azure.core.credentials import AccessToken
from cryptography.fernet import Fernet

from pii_rag.knowledge import Evidence, SearchREST, chunks, evidence_tokens, ingest
from pii_rag.pii import (
    TOKEN,
    AzureDetector,
    PresidioDetector,
    ProtectionError,
    Span,
    SyntheticDetector,
    Vault,
    normalize_spans,
    redact_question,
)


@pytest.fixture
def vault(tmp_path):
    store = Vault(tmp_path / "test.sqlite3", Fernet.generate_key())
    yield store
    store.close()


def test_round_trip_encryption_idempotency_and_collection_scope(vault, tmp_path):
    original = "Alice Martin saw HTTP 504. Contact alice@example.test."
    docs = ingest(vault, SyntheticDetector(), "support", "ticket", original)
    safe = docs[0].content
    assert "Alice" not in safe and "alice@" not in safe
    assert "HTTP 504" in safe
    assert ingest(vault, SyntheticDetector(), "support", "ticket", original) == docs
    allowed = evidence_tokens(docs, "support")
    assert vault.restore("support", safe, allowed) == (original, 0)
    assert "Alice" not in vault.restore("policies", safe, allowed)[0]
    assert vault.restore("support", safe, {})[1] == 2
    assert b"alice@example.test" not in (tmp_path / "test.sqlite3").read_bytes()


def test_stale_document_version_cannot_be_restored(vault):
    old = ingest(vault, SyntheticDetector(), "support", "ticket", "Alice Martin saw errors.")
    ingest(vault, SyntheticDetector(), "support", "ticket", "Bob Chen saw errors.")
    assert vault.restore("support", old[0].content, evidence_tokens(old, "support"))[1] == 1


def test_question_redaction_preserves_technical_relevance():
    question = "Alice Martin reports HTTP 504 at checkout; card 4111 1111 1111 1111."
    safe = redact_question(question, SyntheticDetector())
    assert "HTTP 504 at checkout" in safe
    assert "Alice Martin" not in safe and "4111" not in safe
    assert "[REDACTED_PERSON]" in safe and "[REDACTED_CREDIT_CARD]" in safe


def test_overlaps_unicode_and_token_boundaries(vault):
    assert normalize_spans("0123456789", [Span(2, 6, "A"), Span(4, 9, "B")]) == [Span(2, 9, "A")]
    text = "😀 " + "x " * 580 + "Alice Martin and alice@example.test."
    docs = ingest(vault, SyntheticDetector(), "support", "unicode", text)
    assert all(not ("[PII_" in part and not TOKEN.search(part)) for part in chunks(docs[0].content))
    for doc in docs:
        assert vault.restore("support", doc.content, evidence_tokens(docs, "support"))[1] == 0
    with pytest.raises(ProtectionError):
        normalize_spans("hello", [Span(1, 99, "PERSON")])


@pytest.mark.parametrize("text", ["", " " * 10, "x" * 4001, "[PII_PERSON_deadbeef]"])
def test_invalid_or_forged_source_fails_closed(vault, text):
    with pytest.raises(ProtectionError):
        ingest(vault, SyntheticDetector(), "support", "ticket", text)


class Credential:
    def get_token(self, *scopes, **kwargs):
        return AccessToken("fixture-token", 9999999999)

    def close(self):
        pass


def test_azure_detector_offsets_and_logging_opt_out():
    def respond(request):
        body = json.loads(request.content)
        assert body["parameters"]["stringIndexType"] == "UnicodeCodePoint"
        assert body["parameters"]["loggingOptOut"] is True
        return httpx.Response(
            200,
            json={
                "results": {
                    "documents": [
                        {
                            "id": "1",
                            "warnings": [],
                            "entities": [{"offset": 2, "length": 12, "category": "Person"}],
                        }
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        detector = AzureDetector("https://language.example", Credential(), http)
        assert detector.detect("😀 Alice Martin") == [Span(2, 14, "PERSON")]


def test_azure_warning_is_not_accepted_as_an_empty_detection():
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "results": {
                            "documents": [{"entities": [], "warnings": [{"code": "truncated"}]}]
                        }
                    },
                )
            )
        ) as http,
        pytest.raises(ProtectionError),
    ):
        AzureDetector("https://language.example", Credential(), http).detect("text")


def test_presidio_provider_normalizes_spans():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[{"start": 0, "end": 12, "entity_type": "PERSON", "score": 0.9}],
            )
        )
    ) as http:
        assert PresidioDetector("http://presidio", http).detect("Alice Martin") == [
            Span(0, 12, "PERSON"),
        ]


def test_foundry_iq_uses_fixed_kb_and_requires_scoped_source_data():
    def respond(request):
        assert "/knowledgebases('pii-support-kb')/retrieve" in str(request.url)
        body = json.loads(request.content)
        assert body["intents"] == [{"type": "semantic", "search": "HTTP 504"}]
        assert body["knowledgeSourceParams"][0]["includeReferenceSourceData"] is True
        return httpx.Response(
            200,
            json={
                "references": [
                    {
                        "id": "0",
                        "sourceData": {
                            "collection": "policies",
                            "content": "not permitted",
                            "scope": "doc:1",
                        },
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        search = SearchREST("https://search.example", Credential(), http, "2026-10-01")
        with pytest.raises(ProtectionError):
            search.retrieve("support", "HTTP 504")


def test_cross_collection_evidence_rejected():
    with pytest.raises(ProtectionError):
        evidence_tokens([Evidence("1", "text", "scope", "policies")], "support")


@pytest.mark.parametrize("run_status,expected", [(204, "queued"), (409, "awaiting")])
def test_indexer_busy_is_explicitly_scheduled_not_reported_as_processed(run_status, expected):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(run_status)
        return httpx.Response(200, json={"lastResult": {"status": "inProgress"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        search = SearchREST("https://search.example", Credential(), http, "2026-10-01")
        assert expected in search.run_indexer("support")
