"""Exercise the deployed sample with synthetic data; never substitutes local demo output."""

import argparse
import json
import os
import re
import time
from pathlib import Path

import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from pii_rag.agent import FoundryAgent
from pii_rag.config import COLLECTIONS, Settings, collection_name, required
from pii_rag.knowledge import SearchREST
from pii_rag.pii import TOKEN, AzureDetector, redact_question
from pii_rag.samples import CANARIES, PII_CATEGORIES, SAMPLES, TECHNICAL_FACTS


def verify(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)
    print(f"PASS: {message}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed", action="store_true", help="Append synthetic fixtures using the API"
    )
    parser.add_argument("--timeout", type=int, default=600, help="Indexer wait limit in seconds")
    args = parser.parse_args()
    settings = Settings.load()
    if settings.mode != "azure" or settings.provider != "azure":
        parser.error("This live check requires RAG_MODE=azure and RAG_PII_PROVIDER=azure")
    base = required("RAG_SKILL_BASE_URL")
    headers = {"X-App-Key": settings.app_key}
    report: dict[str, object] = {"api": base, "mode": "azure", "provider": "azure"}
    with AzureCliCredential(process_timeout=60) as credential, httpx.Client(timeout=120) as client:
        search = SearchREST(
            required("RAG_SEARCH_ENDPOINT"),
            credential,
            client,
            os.getenv("RAG_SEARCH_API_VERSION", "2026-10-01"),
        )

        def api(method: str, path: str, body: dict | None = None) -> dict:
            response = client.request(method, base + path, headers=headers, json=body)
            if response.is_error:
                # The sample API only exposes redacted diagnostics; never print request headers.
                raise RuntimeError(f"API {path}: {response.status_code} {response.text[:500]}")
            return response.json() if response.content else {}

        configuration = api("GET", "/api/config")
        verify(
            configuration["mode"] == "azure" and configuration["provider"] == "azure",
            "deployed API uses real Azure mode and Azure PII provider",
        )
        if not args.seed:
            walkthrough = api("GET", "/api/examples/support/INC-104")
            verify(
                walkthrough["source"] == "Azure AI Search"
                and "Alice Martin" in walkthrough["original"]
                and "4111111111111111" in walkthrough["original"]
                and all(
                    value not in "\n".join(walkthrough["indexed_chunks"]) for value in CANARIES
                ),
                "walkthrough compares fictional originals with actual protected Search chunks",
            )
        if args.seed:
            for collection, samples in SAMPLES.items():
                for _, text in samples:
                    result = api("POST", f"/api/collections/{collection}/documents", {"text": text})
                    print(
                        f"Uploaded synthetic {collection} fixture: {result['status']}", flush=True
                    )
        counts = {}
        for collection in COLLECTIONS:
            deadline = time.monotonic() + args.timeout
            while True:
                status = search.request(
                    "GET",
                    f"/indexers/{collection_name(collection, 'indexer')}/status",
                )
                last = status.get("lastResult") or {}
                if last.get("status") in {"transientFailure", "persistentFailure"}:
                    errors = [
                        {
                            "statusCode": e.get("statusCode"),
                            "message": e.get("errorMessage", "")[:600],
                        }
                        for e in last.get("errors", [])
                    ]
                    raise RuntimeError(f"{collection} indexer failed: {json.dumps(errors)}")
                if last.get("status") == "success":
                    docs = search.request(
                        "POST",
                        f"/indexes/{collection_name(collection, 'index')}/docs/search",
                        {
                            "search": "*",
                            "top": 100,
                            "select": "content,scope,collection",
                            "count": True,
                        },
                    )
                    if all(
                        any(
                            source in d["content"] and TECHNICAL_FACTS[source] in d["content"]
                            for d in docs.get("value", [])
                        )
                        for source, _ in SAMPLES[collection]
                    ):
                        break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"{collection} indexer did not finish within {args.timeout}s"
                    )
                if last.get("status") != "inProgress":
                    search.run_indexer(collection)
                print(
                    f"Waiting for {collection} indexer: {last.get('status', 'starting')}",
                    flush=True,
                )
                time.sleep(10)
            values = docs["value"]
            verify(
                all(
                    any(
                        source in d["content"] and TECHNICAL_FACTS[source] in d["content"]
                        for d in values
                    )
                    for source, _ in SAMPLES[collection]
                ),
                f"{collection}: every current sample file's technical fact was indexed",
            )
            protected = json.dumps(values)
            verify(
                all(c.lower() not in protected.lower() for c in CANARIES),
                f"{collection}: no clear synthetic identifiers in indexed fields",
            )
            for category, canaries in PII_CATEGORIES.items():
                verify(
                    all(value.lower() not in protected.lower() for value in canaries),
                    f"{collection}: {category} canaries absent",
                )
            verify(
                all(d["collection"] == collection for d in values),
                f"{collection}: index contains only its bound collection",
            )
            verify(bool(TOKEN.search(protected)), f"{collection}: real PII tokens were indexed")
            verify(
                not last.get("errors") and not last.get("warnings"),
                f"{collection}: managed ingestion completed without errors or warnings",
            )
            counts[collection] = len(values)
        report["indexed_chunks"] = counts

        sessions: list[str] = []
        try:
            support = api("POST", "/api/sessions", {"collection": "support"})["session_id"]
            sessions.append(support)
            answer = api(
                "POST",
                "/api/chat",
                {
                    "session_id": support,
                    "question": "Alice Martin saw HTTP 504 at checkout. Summarize the causes and fixes.",
                },
            )
            verify(
                "Alice Martin" not in answer["sanitized_question"],
                "incoming question is protected before agent invocation",
            )
            verify(
                bool(answer["evidence"])
                and all(d["collection"] == "support" for d in answer["evidence"]),
                "prompt agent called Foundry IQ and returned scoped support evidence",
            )
            verify(
                bool(
                    re.search(r"concurr|backoff|pool|connection", answer["answer"], re.IGNORECASE)
                ),
                "support summary retained relevant remediation facts",
            )
            followup = api(
                "POST",
                "/api/chat",
                {
                    "session_id": support,
                    "question": "Which of those fixes reduce connection contention?",
                },
            )
            verify(
                bool(followup["evidence"]) and bool(followup["answer"]),
                "multi-turn prompt-agent follow-up returned grounded evidence",
            )
            for case, question, expected in (
                (
                    "INC-105",
                    (
                        "Now switch to shipment notifications: what caused duplicate messages "
                        "and how were they prevented?"
                    ),
                    r"idempoten|deduplic|at-least-once",
                ),
                (
                    "INC-106",
                    (
                        "Now switch to account verification HTTP 503: what configuration "
                        "caused the outage and what fixed it?"
                    ),
                    r"timeout|circuit breaker",
                ),
            ):
                technical = api(
                    "POST",
                    "/api/chat",
                    {"session_id": support, "question": question},
                )
                verify(
                    any(case in item["content"] for item in technical["evidence"])
                    and bool(re.search(expected, technical["answer"], re.IGNORECASE))
                    and all(value not in technical["answer"] for value in CANARIES),
                    f"{case}: technical answer stays relevant despite address/payment/ID redaction",
                )
            policy = api("POST", "/api/sessions", {"collection": "policies"})["session_id"]
            sessions.append(policy)
            policy_answer = api(
                "POST",
                "/api/chat",
                {
                    "session_id": policy,
                    "question": "What details should we capture during checkout incidents?",
                },
            )
            verify(
                bool(policy_answer["evidence"])
                and all(d["collection"] == "policies" for d in policy_answer["evidence"]),
                "policy conversation cannot retrieve the support index",
            )
            verify(
                bool(
                    re.search(
                        r"timestamp|correlation|deployment", policy_answer["answer"], re.IGNORECASE
                    )
                ),
                "policy answer retained expected technical guidance",
            )
            quote = api(
                "POST",
                "/api/chat",
                {
                    "session_id": support,
                    "restore": True,
                    "question": "Quote the checkout HTTP 504 incident evidence verbatim, including "
                    "its reporter sentence. Preserve every opaque placeholder token "
                    "exactly; do not infer original identifiers.",
                },
            )
            verify(
                bool(TOKEN.search(quote["sanitized_answer"])),
                "agent returned exact evidence-backed tokens for the restoration check",
            )
            verify(
                quote["answer"] != quote["sanitized_answer"] and not quote["blocked_tokens"],
                "backend restored evidence-backed values only after final generation",
            )
            verify(
                all(c not in quote["sanitized_answer"] for c in CANARIES),
                "pre-restoration answer remains pseudonymized",
            )
            hidden = api(
                "POST",
                "/api/answer/display",
                {
                    "session_id": support,
                    "answer_id": quote["answer_id"],
                    "restore": False,
                },
            )
            revealed = api(
                "POST",
                "/api/answer/display",
                {
                    "session_id": support,
                    "answer_id": quote["answer_id"],
                    "restore": True,
                },
            )
            verify(
                hidden["answer"] == quote["sanitized_answer"]
                and revealed["answer"] == quote["answer"]
                and revealed["privacy"]["restored_occurrences"] > 0,
                "same saved answer switches protected/original without a new agent turn",
            )
            report["chat_checks"] = "passed"
        finally:
            for session in sessions:
                api("DELETE", f"/api/sessions/{session}")

        detector = AzureDetector(
            required("RAG_LANGUAGE_ENDPOINT"),
            credential,
            client,
            os.getenv("RAG_LANGUAGE_API_VERSION", "2025-11-01"),
        )
        with AIProjectClient(
            endpoint=required("RAG_PROJECT_ENDPOINT"), credential=credential
        ) as project:
            agent = FoundryAgent(project, search, detector)
            conversation = agent.create_conversation("support")
            try:
                question = redact_question(
                    "Alice Martin saw HTTP 504; card 4111111111111111 was incidental. "
                    "What technical mitigations are documented?",
                    detector,
                )
                verify(
                    "4111111111111111" not in question,
                    "credit-card canary is removed from questions",
                )
                result = agent.answer("support", conversation, question)
                verify(
                    bool(result.evidence), "direct prompt-agent history check used real IQ evidence"
                )
                with project.get_openai_client(
                    agent_name=collection_name("support", "agent")
                ) as model:
                    history = "\n".join(
                        item.model_dump_json()
                        for item in model.conversations.items.list(conversation_id=conversation)
                    )
                verify(
                    all(c.lower() not in history.lower() for c in CANARIES),
                    "Foundry conversation items contain no clear identifier canaries",
                )
                report["history_canary_check"] = "passed"
            finally:
                agent.delete_conversation("support", conversation)
    output = Path(".data") / "azure-test" / "results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Live Azure checks passed. Non-secret summary: {output}", flush=True)


if __name__ == "__main__":
    main()
