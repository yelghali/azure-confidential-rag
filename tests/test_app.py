from fastapi.testclient import TestClient

from pii_rag.agent import AgentResult
from pii_rag.app import create_app
from pii_rag.config import Settings
from pii_rag.pii import TOKEN


def settings(tmp_path):
    return Settings("demo", tmp_path, "", "", "a" * 32, "azure")


def test_demo_chat_restoration_and_collection_reset(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        cfg = client.get("/api/config").json()
        assert cfg["mode"] == "demo" and "no LLM" in cfg["notice"]
        session = client.post("/api/sessions", json={"collection": "support"}).json()["session_id"]
        first = client.post(
            "/api/chat",
            json={
                "session_id": session,
                "question": "Alice Martin reported checkout HTTP 504.",
            },
        )
        assert first.status_code == 200
        assert "Alice Martin" not in first.json()["sanitized_question"]
        assert "Alice Martin" not in first.json()["answer"]
        assert "HTTP 504" in first.json()["answer"]
        second = client.post(
            "/api/chat",
            json={
                "session_id": session,
                "question": "What fixed checkout?",
                "restore": True,
            },
        ).json()
        assert "Alice Martin" in second["answer"]
        assert "Alice Martin" not in second["sanitized_answer"]
        assert all(doc["collection"] == "support" for doc in second["evidence"])
        policy_session = client.post(
            "/api/sessions",
            json={
                "collection": "policies",
            },
        ).json()["session_id"]
        policy = client.post(
            "/api/chat",
            json={
                "session_id": policy_session,
                "question": "checkout",
            },
        ).json()
        assert all(doc["collection"] == "policies" for doc in policy["evidence"])
        assert client.delete(f"/api/sessions/{session}").status_code == 204
        assert (
            client.post(
                "/api/chat",
                json={
                    "session_id": session,
                    "question": "checkout",
                },
            ).status_code
            == 404
        )


def test_skills_publish_only_sanitized_outputs_and_handle_record_failure(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        body = {
            "values": [
                {
                    "recordId": "1",
                    "data": {"text": "Alice Martin saw HTTP 504.", "document_id": "blob"},
                },
                {"recordId": "2", "data": {"text": "[PII_forged]", "document_id": "blob2"}},
            ]
        }
        assert client.post("/api/skill/support", json=body).status_code == 401
        result = client.post("/api/skill/support", json=body, headers={"X-Skill-Key": "a" * 32})
        assert result.status_code == 200
        values = result.json()["values"]
        assert "Alice Martin" not in result.text
        assert TOKEN.search(values[0]["data"]["chunks"][0]["text"])
        assert values[1]["errors"] and not values[1]["data"]


def test_injected_token_cannot_be_restored_without_retrieved_evidence(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        rt = app.state.runtime
        original = rt.demo.documents["policies"][0]
        token = TOKEN.findall(original.content)[0]
        session_id = client.post("/api/sessions", json={"collection": "support"}).json()[
            "session_id"
        ]
        rt.agent.answer = lambda *args: AgentResult(f"Leaked: {token}", [])
        result = client.post(
            "/api/chat",
            json={
                "session_id": session_id,
                "question": "checkout",
                "restore": True,
            },
        ).json()
        assert result["answer"] == "Leaked: [REDACTED]"
        assert result["blocked_tokens"] == 1


def test_unknown_collection_and_raw_demo_upload_are_rejected(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.post("/api/sessions", json={"collection": "../policies"}).status_code == 404
        assert (
            client.post(
                "/api/collections/support/documents", json={"text": "real data"}
            ).status_code
            == 400
        )
        assert client.post("/api/chat", json={"question": "secret-canary"}).status_code == 422
        assert (
            "secret-canary"
            not in client.post(
                "/api/chat",
                json={
                    "question": "secret-canary",
                },
            ).text
        )
        assert client.get("/").headers["cache-control"] == "no-store"
        assert client.get("/static/app.js").status_code == 200


def test_failed_agent_turn_marks_conversation_unusable(tmp_path):
    import httpx

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        session = client.post("/api/sessions", json={"collection": "support"}).json()["session_id"]

        def fail(*args):
            raise httpx.ConnectError("failure")

        app.state.runtime.agent.answer = fail
        request = {"session_id": session, "question": "checkout"}
        assert client.post("/api/chat", json=request).status_code == 502
        assert client.post("/api/chat", json=request).status_code == 409


def test_access_gate_requires_the_application_key_not_the_skill_key(tmp_path):
    from dataclasses import replace

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        # Exercise only authentication with a fixture runtime; no Azure dependencies are invoked.
        app.state.runtime.settings = replace(
            app.state.runtime.settings,
            mode="azure",
            app_key="application-fixture-key",
        )
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/config").status_code == 401
        assert client.get("/api/config", headers={"X-App-Key": "wrong-key"}).status_code == 401
        assert client.get("/api/config", headers={"X-App-Key": "a" * 32}).status_code == 401
        assert (
            client.get(
                "/api/config",
                headers={"X-App-Key": "application-fixture-key"},
            ).status_code
            == 200
        )


def test_latest_answer_toggle_restores_and_hides_without_another_agent_call(tmp_path):
    from unittest.mock import Mock

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        rt = app.state.runtime
        rt.agent.answer = Mock(wraps=rt.agent.answer)
        sid = client.post("/api/sessions", json={"collection": "support"}).json()["session_id"]
        initial = client.post(
            "/api/chat",
            json={
                "session_id": sid,
                "question": "checkout HTTP 504",
            },
        ).json()
        assert initial["privacy"]["answer_placeholders"] > 0
        body = {"session_id": sid, "answer_id": initial["answer_id"], "restore": True}
        revealed = client.post("/api/answer/display", json=body).json()
        assert "Alice Martin" in revealed["answer"]
        assert revealed["privacy"]["restored_occurrences"] > 0
        assert revealed["sanitized_answer"] == initial["sanitized_answer"]
        hidden = client.post("/api/answer/display", json=body | {"restore": False}).json()
        assert hidden["answer"] == initial["sanitized_answer"]
        assert rt.agent.answer.call_count == 1
        other = client.post("/api/sessions", json={"collection": "policies"}).json()["session_id"]
        assert (
            client.post("/api/answer/display", json=body | {"session_id": other}).status_code == 404
        )
        assert client.post("/api/answer/display", json=body | {"allowed": {}}).status_code == 422


def test_zero_token_summary_distinguishes_protected_evidence_and_no_restoration(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        rt = app.state.runtime
        rt.agent.answer = lambda *args: AgentResult(
            "Reduce concurrency to fix HTTP 504.",
            [rt.demo.documents["support"][0]],
        )
        sid = client.post("/api/sessions", json={"collection": "support"}).json()["session_id"]
        result = client.post(
            "/api/chat",
            json={
                "session_id": sid,
                "question": "Summarize checkout.",
                "restore": True,
            },
        ).json()
        assert result["privacy"]["answer_placeholders"] == 0
        assert result["privacy"]["evidence_placeholders"] > 0
        assert result["privacy"]["restored_occurrences"] == 0
        assert result["answer"] == result["sanitized_answer"]


def test_output_scan_redactions_are_explicit_and_not_falsely_restorable(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        rt = app.state.runtime
        rt.agent.answer = lambda *args: AgentResult("Alice Martin reported HTTP 504.", [])
        sid = client.post("/api/sessions", json={"collection": "support"}).json()["session_id"]
        result = client.post(
            "/api/chat",
            json={
                "session_id": sid,
                "question": "checkout",
                "restore": True,
            },
        ).json()
        assert result["answer"] == "[REDACTED] reported HTTP 504."
        assert result["privacy"]["output_scan_redactions"] == 1
        assert result["privacy"]["output_scan_categories"] == {"PERSON": 1}
        assert result["privacy"]["restored_occurrences"] == 0


def test_walkthrough_only_exposes_bundled_synthetic_inputs_and_indexed_tokens(tmp_path):
    from pii_rag.samples import CANARIES

    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.get("/api/examples/support/INC-104")
        assert response.status_code == 200
        sample = response.json()
        assert sample["synthetic"] is True
        assert sample["source"] == "local fixture, not Azure"
        assert "Alice Martin" in sample["original"] and "4111111111111111" in sample["original"]
        indexed = "\n".join(sample["indexed_chunks"])
        assert all(value not in indexed for value in CANARIES)
        assert sample["pii_counts"]["CREDIT_CARD"] == 1
        assert client.get("/api/examples/support/customer-upload.txt").status_code == 404
        assert client.get("/api/examples/policies/INC-104").status_code == 404


def test_walkthrough_accepts_extracted_trailing_lf_but_not_other_document_versions(tmp_path):
    from pii_rag.knowledge import ingest
    from pii_rag.samples import SAMPLES

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        rt = app.state.runtime
        original = dict(SAMPLES["support"])["INC-104"]
        rt.demo.documents["support"] = ingest(
            rt.vault,
            rt.detector,
            "support",
            "INC-104",
            original + "\n",
        )
        assert client.get("/api/examples/support/INC-104").status_code == 200
        rt.demo.documents["support"] = ingest(
            rt.vault,
            rt.detector,
            "support",
            "INC-104",
            original + " changed content",
        )
        assert client.get("/api/examples/support/INC-104").status_code == 404


def test_guided_pii_question_is_redacted_before_agent_call_and_source_tokens_can_reveal(tmp_path):
    from unittest.mock import Mock

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        rt = app.state.runtime
        rt.agent.answer = Mock(wraps=rt.agent.answer)
        examples = client.get("/api/config").json()["privacy_questions"]["support"]
        sid = client.post("/api/sessions", json={"collection": "support"}).json()["session_id"]
        for example in examples[:2]:
            response = client.post(
                "/api/chat",
                json={
                    "session_id": sid,
                    "question": example["question"],
                },
            )
            assert response.status_code == 200
            result = response.json()
            sent = rt.agent.answer.call_args.args[2]
            assert sent == result["sanitized_question"]
            assert all(
                value not in sent
                for value in (
                    "Alice Martin",
                    "alice@example.test",
                    "4111111111111111",
                )
            )
            assert result["privacy"]["question_redactions"] > 0
        assert result["privacy"]["answer_placeholders"] > 0
        revealed = client.post(
            "/api/answer/display",
            json={
                "session_id": sid,
                "answer_id": result["answer_id"],
                "restore": True,
            },
        ).json()
        assert revealed["privacy"]["restored_occurrences"] > 0
