"""Deploy Demo 2 on Azure using Azure CLI; reuse an existing Foundry account and models."""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def az(*arguments: str, sensitive: bool = False) -> object:
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is required; run az login first")
    print("Azure: " + " ".join(arguments[:3]), flush=True)
    result = subprocess.run(
        [executable, *arguments, "--only-show-errors", "-o", "json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = "Restricted settings operation failed" if sensitive else result.stderr.strip()
        raise RuntimeError(detail)
    return json.loads(result.stdout) if result.stdout.strip() else None


def az_object(*arguments: str) -> dict:
    result = az(*arguments)
    if not isinstance(result, dict):
        raise TypeError("Azure CLI did not return the expected resource object")
    return result


def az_list(*arguments: str) -> list[dict]:
    result = az(*arguments)
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise TypeError("Azure CLI did not return the expected resource list")
    return result


def ensure(show: list[str], create: list[str]) -> dict:
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is required")
    result = subprocess.run(
        [executable, *show, "--only-show-errors", "-o", "json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        resource = json.loads(result.stdout)
        if not isinstance(resource, dict):
            raise TypeError("Azure CLI did not return a resource object")
        return resource
    if not any(
        code in result.stderr
        for code in (
            "ResourceNotFound",
            "ResourceGroupNotFound",
            "NotFound",
            "could not be found",
        )
    ):
        raise RuntimeError(result.stderr.strip())
    return az_object(*create)


def package_app(destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        paths = [
            ROOT / "pyproject.toml",
            ROOT / "requirements.txt",
            *sorted((ROOT / "pii_rag").rglob("*")),
        ]
        for path in paths:
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(ROOT).as_posix())


def write_env_settings(path: Path, values: dict[str, str]) -> None:
    previous = dotenv_values(path) if path.exists() else {}
    if all(previous.get(key) == value for key, value in values.items()):
        return
    content = "".join(
        f"{key}\n" if value is None else f"{key}={json.dumps(value, ensure_ascii=False)}\n"
        for key, value in (previous | values).items()
    )
    staging = path.parent / ".data"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=staging,
        prefix="env-deploy-",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        for attempt in range(5):
            try:
                # Publish once, rather than repeatedly replacing a watched .env for every key.
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                print("Waiting for a transient configuration-file lock...", flush=True)
                time.sleep(0.5 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def grant(principal: str, kind: str, role: str, scope: str) -> None:
    existing = az_list("role", "assignment", "list", "--scope", scope)
    if any(r["principalId"] == principal and r["roleDefinitionName"] == role for r in existing):
        return
    az(
        "role",
        "assignment",
        "create",
        "--assignee-object-id",
        principal,
        "--assignee-principal-type",
        kind,
        "--role",
        role,
        "--scope",
        scope,
    )


def wait_for_api(base: str) -> None:
    deadline = time.monotonic() + 300
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(base + "/api/health")
            except httpx.TransportError:
                print("Waiting for deployed API connectivity...", flush=True)
            else:
                if response.status_code == 200 and response.json().get("mode") == "azure":
                    return
                print(f"Waiting for API startup: HTTP {response.status_code}", flush=True)
            time.sleep(10)
    raise RuntimeError("API did not start in Azure mode within five minutes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--suffix", required=True, help="6-10 lowercase alphanumeric characters")
    parser.add_argument("--foundry-resource-group", required=True)
    parser.add_argument("--foundry-account", required=True)
    parser.add_argument("--location", default="swedencentral")
    parser.add_argument("--storage-location", default="eastus2")
    parser.add_argument("--chat-deployment", default="gpt-4o-mini")
    parser.add_argument("--embedding-deployment", default="text-embedding-3-small")
    parser.add_argument(
        "--accept-costs", action="store_true", help="Acknowledge billable S1/B1 resources"
    )
    args = parser.parse_args()
    if not args.accept_costs:
        parser.error(
            "Pass --accept-costs: this creates billable Search S1, App Service B1 and private endpoints"
        )
    if not re.fullmatch(r"[a-z0-9]{6,10}", args.suffix):
        parser.error("Suffix must contain 6-10 lowercase alphanumeric characters")
    rg, suffix = args.resource_group, args.suffix
    names = {
        "search": f"srch-pii-{suffix}",
        "storage": f"stpiirag{suffix}",
        "plan": f"plan-pii-rag-{suffix}",
        "app": f"app-pii-rag-{suffix}",
        "vnet": f"vnet-pii-{suffix}",
        "project": f"pii-rag-{suffix}",
        "endpoint": f"pe-pii-blob-{suffix}",
    }
    base = f"https://{names['app']}.azurewebsites.net"
    env_path = ROOT / ".env"
    previous = dotenv_values(env_path) if env_path.exists() else {}
    if previous.get("RAG_SKILL_BASE_URL") not in (None, "", base):
        parser.error(
            "Existing .env targets another app. Back it up securely before choosing a new deployment."
        )
    account = az_object("account", "show")
    subscription = account["id"]
    group = ensure(
        ["group", "show", "-n", rg],
        ["group", "create", "-n", rg, "-l", args.location, "--tags", "sample=pii-rag"],
    )
    if (group.get("tags") or {}).get("sample") != "pii-rag":
        raise RuntimeError("Refusing to modify an existing resource group without sample=pii-rag")
    prefix = f"/subscriptions/{subscription}/resourceGroups/{rg}/providers"
    foundry = az_object(
        "cognitiveservices",
        "account",
        "show",
        "-g",
        args.foundry_resource_group,
        "-n",
        args.foundry_account,
    )
    deployments = az_list(
        "cognitiveservices",
        "account",
        "deployment",
        "list",
        "-g",
        args.foundry_resource_group,
        "-n",
        args.foundry_account,
    )
    deployed = {d["name"]: d for d in deployments}
    for model in (args.chat_deployment, args.embedding_deployment):
        if (
            model not in deployed
            or deployed[model]["properties"]["provisioningState"] != "Succeeded"
        ):
            raise RuntimeError(f"Required model deployment is not ready: {model}")
    endpoints = foundry["properties"]["endpoints"]
    project = ensure(
        [
            "cognitiveservices",
            "account",
            "project",
            "show",
            "-g",
            args.foundry_resource_group,
            "-n",
            args.foundry_account,
            "--project-name",
            names["project"],
        ],
        [
            "cognitiveservices",
            "account",
            "project",
            "create",
            "-g",
            args.foundry_resource_group,
            "-n",
            args.foundry_account,
            "--project-name",
            names["project"],
            "-l",
            foundry["location"],
            "--display-name",
            "PII RAG sample",
        ],
    )
    search = ensure(
        ["search", "service", "show", "-g", rg, "-n", names["search"]],
        [
            "search",
            "service",
            "create",
            "-g",
            rg,
            "-n",
            names["search"],
            "-l",
            args.location,
            "--sku",
            "standard",
            "--identity-type",
            "SystemAssigned",
            "--disable-local-auth",
            "true",
            "--semantic-search",
            "free",
        ],
    )
    if search["sku"]["name"] != "standard":
        raise RuntimeError("Private custom skills require S1 here; refusing an implicit SKU change")
    storage = ensure(
        ["storage", "account", "show", "-g", rg, "-n", names["storage"]],
        [
            "storage",
            "account",
            "create",
            "-g",
            rg,
            "-n",
            names["storage"],
            "-l",
            args.storage_location,
            "--sku",
            "Standard_LRS",
            "--kind",
            "StorageV2",
            "--allow-blob-public-access",
            "false",
            "--allow-shared-key-access",
            "false",
            "--public-network-access",
            "Disabled",
            "--min-tls-version",
            "TLS1_2",
        ],
    )
    ensure(
        ["appservice", "plan", "show", "-g", rg, "-n", names["plan"]],
        [
            "appservice",
            "plan",
            "create",
            "-g",
            rg,
            "-n",
            names["plan"],
            "--is-linux",
            "--sku",
            "B1",
            "-l",
            args.location,
        ],
    )
    app = ensure(
        ["webapp", "show", "-g", rg, "-n", names["app"]],
        [
            "webapp",
            "create",
            "-g",
            rg,
            "-n",
            names["app"],
            "--plan",
            names["plan"],
            "--runtime",
            "PYTHON:3.12",
            "--assign-identity",
            "[system]",
            "--https-only",
            "true",
        ],
    )
    if not all(previous.get(key) for key in ("RAG_MAPPING_KEY", "RAG_APP_KEY", "RAG_SKILL_KEY")):
        configured_names = az(
            "webapp",
            "config",
            "appsettings",
            "list",
            "-g",
            rg,
            "-n",
            names["app"],
            "--query",
            "[].name",
        )
        if not isinstance(configured_names, list):
            raise TypeError("Could not verify existing app settings")
        if "RAG_MAPPING_KEY" in configured_names:
            raise RuntimeError(
                "Existing app already has mapping secrets. Restore its .env securely "
                "before redeploying; refusing to replace keys and break existing mappings."
            )
    ensure(
        ["network", "vnet", "show", "-g", rg, "-n", names["vnet"]],
        [
            "network",
            "vnet",
            "create",
            "-g",
            rg,
            "-n",
            names["vnet"],
            "-l",
            args.location,
            "--address-prefixes",
            "10.186.0.0/16",
            "--subnet-name",
            "app-integration",
            "--subnet-prefixes",
            "10.186.0.0/26",
        ],
    )
    az(
        "network",
        "vnet",
        "subnet",
        "update",
        "-g",
        rg,
        "--vnet-name",
        names["vnet"],
        "-n",
        "app-integration",
        "--delegations",
        "Microsoft.Web/serverFarms",
    )
    ensure(
        [
            "network",
            "vnet",
            "subnet",
            "show",
            "-g",
            rg,
            "--vnet-name",
            names["vnet"],
            "-n",
            "private-endpoints",
        ],
        [
            "network",
            "vnet",
            "subnet",
            "create",
            "-g",
            rg,
            "--vnet-name",
            names["vnet"],
            "-n",
            "private-endpoints",
            "--address-prefixes",
            "10.186.1.0/27",
            "--private-endpoint-network-policies",
            "Disabled",
        ],
    )
    az(
        "webapp",
        "vnet-integration",
        "add",
        "-g",
        rg,
        "-n",
        names["app"],
        "--vnet",
        names["vnet"],
        "--subnet",
        "app-integration",
    )
    ensure(
        ["network", "private-endpoint", "show", "-g", rg, "-n", names["endpoint"]],
        [
            "network",
            "private-endpoint",
            "create",
            "-g",
            rg,
            "-n",
            names["endpoint"],
            "-l",
            args.location,
            "--vnet-name",
            names["vnet"],
            "--subnet",
            "private-endpoints",
            "--private-connection-resource-id",
            storage["id"],
            "--group-id",
            "blob",
            "--connection-name",
            "pii-api-blob",
        ],
    )
    zone = "privatelink.blob.core.windows.net"
    ensure(
        ["network", "private-dns", "zone", "show", "-g", rg, "-n", zone],
        ["network", "private-dns", "zone", "create", "-g", rg, "-n", zone],
    )
    ensure(
        [
            "network",
            "private-dns",
            "link",
            "vnet",
            "show",
            "-g",
            rg,
            "-n",
            "pii-blob-dns",
            "-z",
            zone,
        ],
        [
            "network",
            "private-dns",
            "link",
            "vnet",
            "create",
            "-g",
            rg,
            "-n",
            "pii-blob-dns",
            "--zone-name",
            zone,
            "--virtual-network",
            names["vnet"],
            "--registration-enabled",
            "false",
        ],
    )
    ensure(
        [
            "network",
            "private-endpoint",
            "dns-zone-group",
            "show",
            "-g",
            rg,
            "--endpoint-name",
            names["endpoint"],
            "-n",
            "blob-zone",
        ],
        [
            "network",
            "private-endpoint",
            "dns-zone-group",
            "create",
            "-g",
            rg,
            "--endpoint-name",
            names["endpoint"],
            "-n",
            "blob-zone",
            "--private-dns-zone",
            f"{prefix}/Microsoft.Network/privateDnsZones/{zone}",
            "--zone-name",
            "blob",
        ],
    )
    for collection in ("support", "policies"):
        az(
            "storage",
            "container-rm",
            "create",
            "-g",
            rg,
            "--storage-account",
            names["storage"],
            "-n",
            f"pii-{collection}-raw",
        )
    app_id, search_id = app["identity"]["principalId"], search["identity"]["principalId"]
    user = az_object("ad", "signed-in-user", "show")["id"]
    for principal, kind, role, scope in (
        (app_id, "ServicePrincipal", "Search Index Data Reader", search["id"]),
        (app_id, "ServicePrincipal", "Search Service Contributor", search["id"]),
        (app_id, "ServicePrincipal", "Storage Blob Data Contributor", storage["id"]),
        (app_id, "ServicePrincipal", "Cognitive Services User", foundry["id"]),
        (app_id, "ServicePrincipal", "Foundry Project Runtime User", project["id"]),
        (search_id, "ServicePrincipal", "Storage Blob Data Reader", storage["id"]),
        (search_id, "ServicePrincipal", "Cognitive Services OpenAI User", foundry["id"]),
        (user, "User", "Search Service Contributor", search["id"]),
        (user, "User", "Search Index Data Reader", search["id"]),
        (user, "User", "Foundry User", project["id"]),
    ):
        grant(principal, kind, role, scope)
    ensure(
        [
            "search",
            "shared-private-link-resource",
            "show",
            "-g",
            rg,
            "--service-name",
            names["search"],
            "-n",
            "private-blob",
        ],
        [
            "search",
            "shared-private-link-resource",
            "create",
            "-g",
            rg,
            "--service-name",
            names["search"],
            "-n",
            "private-blob",
            "--resource-id",
            storage["id"],
            "--group-id",
            "blob",
            "--request-message",
            f"PII RAG {suffix} private ingestion",
        ],
    )
    deadline = time.monotonic() + 300
    while True:
        link = az_object(
            "search",
            "shared-private-link-resource",
            "show",
            "-g",
            rg,
            "--service-name",
            names["search"],
            "-n",
            "private-blob",
        )
        if (
            link["properties"]["status"] == "Approved"
            and link["properties"]["provisioningState"] == "Succeeded"
        ):
            break
        pending = [
            item
            for item in az_list(
                "network", "private-endpoint-connection", "list", "--id", storage["id"]
            )
            if item["properties"]["privateLinkServiceConnectionState"]["status"] == "Pending"
        ]
        if len(pending) > 1:
            raise RuntimeError(
                "Multiple pending private connections; review and approve the Search link explicitly"
            )
        if pending:
            az(
                "network",
                "private-endpoint-connection",
                "approve",
                "--id",
                pending[0]["id"],
                "--description",
                "Approve sample Search private ingestion",
            )
        if time.monotonic() >= deadline:
            raise RuntimeError("Search private link did not become approved/ready")
        time.sleep(10)
    values = {
        "RAG_MODE": "azure",
        "RAG_DATA_DIR": str(ROOT / ".data" / "azure-test"),
        "RAG_MAPPING_KEY": previous.get("RAG_MAPPING_KEY") or Fernet.generate_key().decode(),
        "RAG_APP_KEY": previous.get("RAG_APP_KEY") or secrets.token_urlsafe(32),
        "RAG_SKILL_KEY": previous.get("RAG_SKILL_KEY") or secrets.token_urlsafe(32),
        "RAG_SEARCH_ENDPOINT": f"https://{names['search']}.search.windows.net",
        "RAG_SEARCH_API_VERSION": "2026-10-01",
        "RAG_PROJECT_ENDPOINT": project["properties"]["endpoints"]["AI Foundry API"].rstrip("/"),
        "RAG_MODEL_DEPLOYMENT": args.chat_deployment,
        "RAG_LANGUAGE_ENDPOINT": foundry["properties"]["endpoint"].rstrip("/"),
        "RAG_LANGUAGE_API_VERSION": "2025-11-01",
        "RAG_PII_PROVIDER": "azure",
        "RAG_BLOB_ACCOUNT_URL": storage["primaryEndpoints"]["blob"].rstrip("/"),
        "RAG_STORAGE_RESOURCE_ID": storage["id"],
        "RAG_SKILL_BASE_URL": base,
        "RAG_OPENAI_ENDPOINT": endpoints["OpenAI Language Model Instance API"].rstrip("/"),
        "RAG_EMBEDDING_DEPLOYMENT": args.embedding_deployment,
        "RAG_EMBEDDING_MODEL": deployed[args.embedding_deployment]["properties"]["model"]["name"],
        "RAG_EMBEDDING_DIMENSIONS": "1536",
    }
    write_env_settings(env_path, values)
    with tempfile.TemporaryDirectory(prefix="pii-rag-deploy-") as directory:
        temp = Path(directory)
        settings = temp / "settings.json"
        settings.write_text(
            json.dumps(
                values
                | {
                    "RAG_DATA_DIR": "/home/data/pii-rag",
                    "SCM_DO_BUILD_DURING_DEPLOYMENT": "true",
                    "ENABLE_ORYX_BUILD": "true",
                    "PYTHONUNBUFFERED": "1",
                }
            ),
            encoding="utf-8",
        )
        az(
            "webapp",
            "config",
            "appsettings",
            "set",
            "-g",
            rg,
            "-n",
            names["app"],
            "--settings",
            "@" + str(settings),
            sensitive=True,
        )
        az(
            "webapp",
            "config",
            "set",
            "-g",
            rg,
            "-n",
            names["app"],
            "--startup-file",
            "python -m uvicorn pii_rag.app:app --host 0.0.0.0 --port 8000 --no-access-log",
        )
        archive = temp / "app.zip"
        package_app(archive)
        az(
            "webapp",
            "deploy",
            "-g",
            rg,
            "-n",
            names["app"],
            "--src-path",
            str(archive),
            "--type",
            "zip",
        )
    wait_for_api(base)
    env = os.environ | values
    subprocess.run(
        [sys.executable, "-m", "scripts.provision", "--skip-storage", "--private-indexer"],
        cwd=ROOT,
        env=env,
        check=True,
    )
    print(
        f"Demo 2 deployed: {base}\nSecrets are in ignored .env; never commit them.\n"
        "Next: python -m scripts.check_azure --seed\n"
        "Search S1, App Service B1 and private networking remain billable.",
        flush=True,
    )


if __name__ == "__main__":
    main()
