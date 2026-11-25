from scripts.provision import definitions


def test_embedding_and_projection_inputs_are_sanitized_only(monkeypatch):
    for name in (
        "RAG_OPENAI_ENDPOINT",
        "RAG_EMBEDDING_DEPLOYMENT",
        "RAG_STORAGE_RESOURCE_ID",
        "RAG_SKILL_BASE_URL",
        "RAG_SKILL_KEY",
    ):
        monkeypatch.setenv(name, "https://fixture.example")
    objects = definitions("support")
    skills = objects["/skillsets/pii-support-skills"]
    assert skills["skills"][1]["inputs"][0]["source"] == "/document/protected_chunks/*/text"
    projections = skills["indexProjections"]
    assert projections["parameters"]["projectionMode"] == "skipIndexingParentDocuments"
    assert all(
        m["source"].startswith("/document/protected_chunks/")
        for m in projections["selectors"][0]["mappings"]
    )
    datasource = objects["/datasources/pii-support-blob"]
    assert "indexerPermissionOptions" not in datasource
    kb = objects["/knowledgebases/pii-support-kb"]
    assert kb["knowledgeSources"] == [{"name": "pii-support-source"}]
    private = definitions("support", private_indexer=True)
    config = private["/indexers/pii-support-indexer"]["parameters"]["configuration"]
    assert config["executionEnvironment"] == "private"
    assert (
        "executionEnvironment"
        not in objects["/indexers/pii-support-indexer"]["parameters"]["configuration"]
    )
