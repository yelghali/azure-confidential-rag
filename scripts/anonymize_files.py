"""Demo 1: a list of UTF-8 files -> real PII API -> irreversibly redacted output files."""

import argparse
import json
import os
from contextlib import ExitStack
from pathlib import Path

import httpx
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from pii_rag.config import required
from pii_rag.deidentify import redact_document
from pii_rag.pii import AzureDetector, Detector, PresidioDetector, ProtectionError

MAX_FILE_BYTES = 5 * 1024 * 1024


def process_files(
    inputs: list[Path],
    output: Path,
    detector: Detector,
    provider: str,
) -> dict:
    if not inputs:
        raise ValueError("No input text files selected")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty; existing files are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for index, source in enumerate(inputs, 1):
        filename = f"document-{index:04d}.txt"
        temporary = output / f".{filename}.tmp"
        try:
            if source.suffix.lower() != ".txt":
                raise ProtectionError("Only UTF-8 .txt files are supported")
            with source.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ProtectionError("File exceeds the 5 MiB limit")
            result = redact_document(data.decode("utf-8-sig"), detector)
            temporary.write_text(result.text, encoding="utf-8", newline="")
            temporary.replace(output / filename)
            records.append(
                {
                    "output": filename,
                    "status": "redacted",
                    "entity_counts": result.counts,
                    "api_calls": result.api_calls,
                }
            )
        except (OSError, UnicodeError, ProtectionError, httpx.HTTPError, AzureError) as error:
            temporary.unlink(missing_ok=True)
            # Exception messages and source paths can contain PII; report ordinal and error type only.
            records.append({"document": index, "status": "failed", "error": type(error).__name__})
        print(f"Document {index}/{len(inputs)}: {records[-1]['status']}", flush=True)
    manifest = {
        "demo": "standalone-pii",
        "provider": provider,
        "policy": "irreversible-category-redaction-v1",
        "notice": "Detected PII was removed. Detection is not guaranteed legal anonymization. "
        "No originals, filenames, entity values, or restoration mappings are exported.",
        "failed": sum(record["status"] == "failed" for record in records),
        "documents": records,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--files", type=Path, nargs="+", help="Explicit list of input .txt files")
    source.add_argument("--input-dir", type=Path, help="Recursively process .txt files in a folder")
    parser.add_argument("--output-dir", type=Path, required=True, help="New or empty destination")
    parser.add_argument("--provider", choices=["azure", "presidio"], default="azure")
    parser.add_argument("--endpoint", help="Override the configured provider endpoint")
    args = parser.parse_args()
    load_dotenv()
    inputs = args.files if args.files is not None else sorted(args.input_dir.rglob("*.txt"))
    output = args.output_dir.resolve()
    if any(path.resolve().is_relative_to(output) for path in inputs):
        parser.error("Output must not contain input files")
    if not inputs:
        parser.error("No input .txt files found")
    with ExitStack() as stack:
        http = stack.enter_context(httpx.Client(timeout=60, follow_redirects=False))
        detector: Detector
        if args.provider == "azure":
            credential = stack.enter_context(DefaultAzureCredential(process_timeout=60))
            detector = AzureDetector(
                args.endpoint or required("RAG_LANGUAGE_ENDPOINT"),
                credential,
                http,
                os.getenv("RAG_LANGUAGE_API_VERSION", "2025-11-01"),
            )
        else:
            detector = PresidioDetector(
                args.endpoint or required("RAG_PRESIDIO_ENDPOINT"),
                http,
            )
        manifest = process_files(inputs, output, detector, args.provider)
    if manifest["failed"]:
        raise SystemExit(
            "Some files failed. No raw-data fallback was written; inspect manifest.json."
        )
    print(f"Demo 1 complete: {len(inputs)} de-identified files in {output}", flush=True)


if __name__ == "__main__":
    main()
