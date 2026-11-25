from collections import Counter
from dataclasses import dataclass

from pii_rag.pii import MAX_TEXT, Detector, ProtectionError, Span, normalize_spans


@dataclass(frozen=True)
class RedactedDocument:
    text: str
    counts: dict[str, int]
    api_calls: int


def redact_document(text: str, detector: Detector) -> RedactedDocument:
    """Irreversible replacement; deliberately does not create token mappings."""
    if not text.strip():
        raise ProtectionError("Empty documents are not supported")
    if "[PII_" in text or "[REDACTED_" in text:
        raise ProtectionError("Reserved placeholder syntax in source document")
    spans = []
    calls = 0
    start = 0
    while True:
        end = min(start + MAX_TEXT, len(text))
        window = text[start:end]
        for span in normalize_spans(window, detector.detect(window)):
            spans.append(Span(start + span.start, start + span.end, span.category))
        calls += 1
        if end == len(text):
            break
        # Reanalyze boundaries with context, then merge overlapping detections in source coordinates.
        start = end - 512
    resolved = normalize_spans(text, spans)
    counts = dict(Counter(span.category for span in resolved))
    for span in reversed(resolved):
        text = text[: span.start] + f"[REDACTED_{span.category}]" + text[span.end :]
    return RedactedDocument(text, counts, calls)
