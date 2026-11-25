import json
import re
from pathlib import Path

import httpx
import pytest

from pii_rag.deidentify import redact_document
from pii_rag.pii import ProtectionError, Span, SyntheticDetector
from pii_rag.samples import CANARIES, SAMPLE_ROOT
from scripts.anonymize_files import process_files


def test_standalone_files_remove_canaries_without_maps_or_original_filenames(tmp_path):
    inputs = sorted(SAMPLE_ROOT.rglob("*.txt"))
    output = tmp_path / "output"
    manifest = process_files(inputs, output, SyntheticDetector(), "fixture-test")
    assert manifest["failed"] == 0
    assert len(list(output.glob("*.txt"))) == len(inputs)
    exported = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
    assert all(value not in exported for value in CANARIES)
    assert "[PII_" not in exported
    assert "HTTP 504" in exported and "circuit breaker" in exported
    assert not list(output.glob("*.sqlite*")) and not list(output.glob("*.key"))
    assert "INC-101.txt" not in json.dumps(manifest)


def test_windowed_detection_covers_pii_crossing_api_limit():
    class Detector:
        def detect(self, text):
            return [Span(m.start(), m.end(), "PERSON") for m in re.finditer("Alice Martin", text)]

    source = "x" * 3995 + "Alice Martin" + " rest" * 1000
    result = redact_document(source, Detector())
    assert "Alice Martin" not in result.text
    assert result.counts == {"PERSON": 1}
    assert result.api_calls > 1


def test_one_failed_file_never_exports_its_raw_text(tmp_path):
    class FailingDetector:
        def detect(self, text):
            raise httpx.ConnectError("Do not echo this source value: Alice Martin")

    source = tmp_path / "Alice-Martin.txt"
    source.write_text("Alice Martin", encoding="utf-8")
    output = tmp_path / "output"
    manifest = process_files([source], output, FailingDetector(), "azure")
    assert manifest["failed"] == 1
    assert list(output.iterdir()) == [output / "manifest.json"]
    assert "Alice" not in json.dumps(manifest)


def test_output_refuses_to_overwrite_an_existing_export(tmp_path):
    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        process_files([Path("not-read.txt")], tmp_path, SyntheticDetector(), "fixture-test")


def test_standalone_rejects_reserved_token_input():
    with pytest.raises(ProtectionError):
        redact_document("Already [PII_PERSON_forged]", SyntheticDetector())


def test_standalone_preserves_crlf_without_doubling_carriage_returns(tmp_path):
    source = tmp_path / "input.txt"
    source.write_bytes(b"Alice Martin reported HTTP 504.\r\nApply exponential backoff.\r\n")
    output = tmp_path / "output"
    result = process_files([source], output, SyntheticDetector(), "fixture-test")
    assert result["failed"] == 0
    content = (output / "document-0001.txt").read_bytes()
    assert b"\r\r\n" not in content
    assert content.count(b"\r\n") == 2
    assert b"Alice Martin" not in content
