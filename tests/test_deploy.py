import zipfile
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values

from scripts import deploy_azure


def test_deployment_archive_includes_sample_files_but_not_secrets(tmp_path):
    archive = tmp_path / "app.zip"
    deploy_azure.package_app(archive)
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
    assert (
        len(
            [
                name
                for name in names
                if name.startswith("pii_rag/sample_data/") and name.endswith(".txt")
            ]
        )
        == 8
    )
    assert ".env" not in names
    assert not any(".data" in name or ".key" in name or ".sqlite" in name for name in names)


def test_deployer_does_not_treat_authorization_failure_as_missing_resource(monkeypatch):
    monkeypatch.setattr(deploy_azure.shutil, "which", lambda name: "az")
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stderr="AuthorizationFailed", stdout="")

    monkeypatch.setattr(deploy_azure.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="AuthorizationFailed"):
        deploy_azure.ensure(["group", "show"], ["group", "create"])
    assert len(calls) == 1


def test_sensitive_cli_failure_never_echoes_setting_values(monkeypatch):
    monkeypatch.setattr(deploy_azure.shutil, "which", lambda name: "az")
    monkeypatch.setattr(
        deploy_azure.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stderr="failure containing secret-canary",
            stdout="",
        ),
    )
    with pytest.raises(RuntimeError) as error:
        deploy_azure.az("webapp", "config", "appsettings", "set", sensitive=True)
    assert "secret-canary" not in str(error.value)


def test_env_update_retries_only_transient_permission_errors(monkeypatch, tmp_path):
    attempts = []
    real_replace = deploy_azure.os.replace

    def replace(source, target):
        args = (source, target)
        attempts.append(args)
        if len(attempts) == 1:
            raise PermissionError("Temporary file replacement lock")
        real_replace(source, target)

    monkeypatch.setattr(deploy_azure.os, "replace", replace)
    monkeypatch.setattr(deploy_azure.time, "sleep", lambda seconds: None)
    deploy_azure.write_env_settings(tmp_path / ".env", {"RAG_MODE": "azure"})
    assert len(attempts) == 2
    assert dotenv_values(tmp_path / ".env")["RAG_MODE"] == "azure"


def test_env_settings_preserve_values_and_skip_unchanged_redeployment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    values = {"RAG_MODE": "azure", "RAG_MAPPING_KEY": r"test\value='example'"}
    deploy_azure.write_env_settings(path, values)
    assert all(dotenv_values(path)[key] == value for key, value in values.items())
    monkeypatch.setattr(
        deploy_azure.os,
        "replace",
        lambda *args: pytest.fail("An unchanged configuration should not be replaced"),
    )
    deploy_azure.write_env_settings(path, values)
