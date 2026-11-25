import json
from pathlib import Path

from pii_rag.config import COLLECTIONS

SAMPLE_ROOT = Path(__file__).with_name("sample_data")
SAMPLES = {
    collection: [
        (path.stem, path.read_text(encoding="utf-8").strip())
        for path in sorted((SAMPLE_ROOT / collection).glob("*.txt"))
    ]
    for collection in COLLECTIONS
}
_EXPECTATIONS = json.loads((SAMPLE_ROOT / "expected_pii.json").read_text(encoding="utf-8"))
PII_CATEGORIES: dict[str, list[str]] = _EXPECTATIONS["categories"]
TECHNICAL_FACTS: dict[str, str] = _EXPECTATIONS["technical_facts"]
CANARIES = [value for values in PII_CATEGORIES.values() for value in values]

PRIVACY_QUESTIONS = {
    "support": [
        {
            "label": "1. Mask my question (name + email)",
            "question": "Alice Martin (alice@example.test) reported checkout HTTP 504. "
            "What technical causes and fixes are documented? Do not identify the person.",
            "expect": "Your name/email disappear from Question sent to agent. "
            "A technical answer may have no PII to reveal.",
        },
        {
            "label": "2. Mask my question (test card) + show source tokens",
            "question": "My test card is 4111111111111111; checkout returned HTTP 429. "
            "Quote the full matching incident evidence verbatim, preserving every opaque "
            "placeholder exactly. Do not infer original identifiers.",
            "expect": "The card disappears from the question sent to the agent. "
            "The quoted source has PII tokens; reveal changes only the answer display.",
        },
        {
            "label": "3. Quote masked password-reset evidence",
            "question": "Quote the full password-reset mail queue incident evidence verbatim, "
            "including its reported details. Preserve every opaque placeholder exactly. "
            "Do not infer original identifiers.",
            "expect": "The source quotation includes hidden name/email tokens. "
            "Reveal uses source mappings, not the contents of your question.",
        },
    ],
    "policies": [
        {
            "label": "1. Mask my question (name + email)",
            "question": "Alice Martin (alice@example.test) asks: what details should we capture "
            "during checkout incidents? Do not identify the person.",
            "expect": "The question's name/email are redacted before the model. "
            "Policy guidance can be readable without revealing anyone.",
        },
        {
            "label": "2. Quote masked policy evidence",
            "question": "Quote the checkout incident documentation policy evidence verbatim, "
            "including its synthetic contact sentence. Preserve every opaque placeholder "
            "exactly. Do not infer original identifiers.",
            "expect": "The quoted contact remains tokenized until you explicitly reveal the answer.",
        },
    ],
}

SUGGESTIONS = {
    "support": [
        "Summarize the checkout incidents and their fixes.",
        "Find incidents similar to database connection exhaustion.",
        "What monitoring improvements are recommended?",
    ],
    "policies": [
        "What should we capture during checkout incidents?",
        "How should password reset delays be investigated?",
    ],
}
