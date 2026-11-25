"""Provision sample-owned Search objects and Foundry prompt agents on existing resources."""

import argparse
import os

import httpx
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from pii_rag.agent import INSTRUCTIONS, TOOL_PARAMETERS
from pii_rag.config import COLLECTIONS, Settings, collection_name, required
from pii_rag.knowledge import SearchREST
from pii_rag.samples import SAMPLES


def definitions(collection: str, *, private_indexer: bool = False) -> dict[str, dict]:
    index = collection_name(collection, "index")
    skillset = collection_name(collection, "skills")
    source = collection_name(collection, "source")
    vector_params = {
        "resourceUri": required("RAG_OPENAI_ENDPOINT"),
        "deploymentId": required("RAG_EMBEDDING_DEPLOYMENT"),
        "modelName": os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small"),
    }
    dimensions = int(os.getenv("RAG_EMBEDDING_DIMENSIONS", "1536"))
    return {
        f"/indexes/{index}": {
            "name": index,
            "fields": [
                {"name": "chunk_id", "type": "Edm.String", "key": True, "analyzer": "keyword"},
                {"name": "parent_id", "type": "Edm.String", "filterable": True},
                {"name": "content", "type": "Edm.String", "searchable": True},
                {"name": "scope", "type": "Edm.String", "filterable": True},
                {"name": "collection", "type": "Edm.String", "filterable": True},
                {
                    "name": "vector",
                    "type": "Collection(Edm.Single)",
                    "searchable": True,
                    "retrievable": False,
                    "dimensions": dimensions,
                    "vectorSearchProfile": "profile",
                },
            ],
            "vectorSearch": {
                "algorithms": [{"name": "hnsw", "kind": "hnsw"}],
                "profiles": [
                    {
                        "name": "profile",
                        "algorithm": "hnsw",
                        "vectorizer": "vectorizer",
                    }
                ],
                "vectorizers": [
                    {
                        "name": "vectorizer",
                        "kind": "azureOpenAI",
                        "azureOpenAIParameters": vector_params,
                    }
                ],
            },
            "semantic": {
                "configurations": [
                    {
                        "name": "semantic",
                        "prioritizedFields": {
                            "prioritizedContentFields": [{"fieldName": "content"}]
                        },
                    }
                ]
            },
        },
        f"/datasources/{collection_name(collection, 'blob')}": {
            "name": collection_name(collection, "blob"),
            "type": "azureblob",
            "credentials": {
                "connectionString": f"ResourceId={required('RAG_STORAGE_RESOURCE_ID')};"
            },
            "container": {"name": collection_name(collection, "raw")},
        },
        f"/skillsets/{skillset}": {
            "name": skillset,
            "description": "PII protection precedes every embedding and index projection.",
            "skills": [
                {
                    "@odata.type": "#Microsoft.Skills.Custom.WebApiSkill",
                    "name": "protect",
                    "context": "/document",
                    "uri": f"{required('RAG_SKILL_BASE_URL')}/api/skill/{collection}",
                    "httpMethod": "POST",
                    "timeout": "PT230S",
                    "batchSize": 1,
                    "degreeOfParallelism": 1,
                    "httpHeaders": {"X-Skill-Key": required("RAG_SKILL_KEY")},
                    "inputs": [
                        {"name": "text", "source": "/document/content"},
                        {"name": "document_id", "source": "/document/metadata_storage_path"},
                    ],
                    "outputs": [{"name": "chunks", "targetName": "protected_chunks"}],
                },
                {
                    "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
                    "name": "embed-protected-content",
                    "context": "/document/protected_chunks/*",
                    **vector_params,
                    "dimensions": dimensions,
                    "inputs": [{"name": "text", "source": "/document/protected_chunks/*/text"}],
                    "outputs": [{"name": "embedding", "targetName": "embedding"}],
                },
            ],
            "indexProjections": {
                "selectors": [
                    {
                        "targetIndexName": index,
                        "parentKeyFieldName": "parent_id",
                        "sourceContext": "/document/protected_chunks/*",
                        "mappings": [
                            {"name": "content", "source": "/document/protected_chunks/*/text"},
                            {"name": "scope", "source": "/document/protected_chunks/*/scope"},
                            {
                                "name": "collection",
                                "source": "/document/protected_chunks/*/collection",
                            },
                            {"name": "vector", "source": "/document/protected_chunks/*/embedding"},
                        ],
                    }
                ],
                "parameters": {"projectionMode": "skipIndexingParentDocuments"},
            },
        },
        f"/indexers/{collection_name(collection, 'indexer')}": {
            "name": collection_name(collection, "indexer"),
            "dataSourceName": collection_name(collection, "blob"),
            "targetIndexName": index,
            "skillsetName": skillset,
            "schedule": {"interval": "PT5M"},
            "parameters": {
                "batchSize": 1,
                "maxFailedItems": 0,
                "maxFailedItemsPerBatch": 0,
                "configuration": {
                    "dataToExtract": "contentAndMetadata",
                    "parsingMode": "default",
                    "indexedFileNameExtensions": ".txt",
                    **({"executionEnvironment": "private"} if private_indexer else {}),
                },
            },
        },
        f"/knowledgesources/{source}": {
            "name": source,
            "kind": "searchIndex",
            "description": f"Sanitized {collection} corpus; incidental PII is pseudonymized.",
            "searchIndexParameters": {
                "searchIndexName": index,
                "semanticConfigurationName": "semantic",
                "sourceDataFields": [{"name": n} for n in ("content", "scope", "collection")],
                "searchFields": [{"name": "content"}],
            },
        },
        f"/knowledgebases/{collection_name(collection, 'kb')}": {
            "name": collection_name(collection, "kb"),
            "knowledgeSources": [{"name": source}],
            "retrievalReasoningEffort": {"kind": "minimal"},
            "outputMode": "extractiveData",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true", help="Upload synthetic fixtures after setup")
    parser.add_argument(
        "--skip-storage",
        action="store_true",
        help="Use containers prepared through ARM; do not call the private Blob data plane locally",
    )
    parser.add_argument(
        "--private-indexer",
        action="store_true",
        help="Use an approved Search shared private link (custom skills require S1 or higher)",
    )
    args = parser.parse_args()
    if args.seed and args.skip_storage:
        parser.error(
            "--seed requires Blob connectivity; use the deployed upload API for private storage"
        )
    settings = Settings.load()
    if settings.mode != "azure":
        parser.error("Set RAG_MODE=azure before provisioning")
    if not required("RAG_SKILL_BASE_URL").startswith("https://"):
        parser.error("RAG_SKILL_BASE_URL must use HTTPS")
    with (
        DefaultAzureCredential() as credential,
        httpx.Client(timeout=90, follow_redirects=False) as http,
        BlobServiceClient(required("RAG_BLOB_ACCOUNT_URL"), credential=credential) as blobs,
        AIProjectClient(required("RAG_PROJECT_ENDPOINT"), credential=credential) as project,
    ):
        search = SearchREST(
            required("RAG_SEARCH_ENDPOINT"),
            credential,
            http,
            os.getenv("RAG_SEARCH_API_VERSION", "2026-10-01"),
        )
        for collection in COLLECTIONS:
            container = blobs.get_container_client(collection_name(collection, "raw"))
            if not args.skip_storage and not container.exists():
                container.create_container()
            for path, body in definitions(collection, private_indexer=args.private_indexer).items():
                search.request("PUT", path, body)
                print(f"Configured {path}")
            agent = project.agents.create_version(
                agent_name=collection_name(collection, "agent"),
                definition=PromptAgentDefinition(
                    model=required("RAG_MODEL_DEPLOYMENT"),
                    instructions=INSTRUCTIONS,
                    tools=[
                        FunctionTool(
                            name="retrieve_knowledge",
                            description="Retrieve technical evidence from this collection's Foundry IQ KB.",
                            parameters=TOOL_PARAMETERS,
                            strict=False,
                        )
                    ],
                ),
            )
            print(f"Created prompt agent {agent.name} version {agent.version}")
            if args.seed:
                for source, text in SAMPLES[collection]:
                    # Append-only, opaque blob names in the sample; no replace/delete lifecycle.
                    blob = container.get_blob_client(f"{source}.txt")
                    if not blob.exists():
                        blob.upload_blob(text.encode())
                print(f"{collection}: {search.run_indexer(collection)}")
    print("Provisioned. Check indexer status in the UI before asking questions.")


if __name__ == "__main__":
    main()
