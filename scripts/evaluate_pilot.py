"""Evaluate CI extraction pilot predictions against manual references."""

import csv
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "data/processed/pilot"

REFERENCES = PILOT / "annotations/pilot_reference.jsonl"

MODELS = {
    "GPT-6.1 Sol": PILOT / "results/openai_dedfefd511.jsonl",
    "Claude Sonnet 5.5": PILOT / "results/anthropic_c13e666820.jsonl",
}

OUTPUT = PILOT / "evaluation"
OUTPUT.mkdir(parents=True, exist_ok=True)

FIELDS = [
    "sender",
    "subject",
    "recipient",
    "information_type",
    "transmission_principle",
    "domain",
]


def load_jsonl(path):
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def normalize(value):
    """Normalize superficial differences in wording."""
    if value is None:
        return ""

    text = str(value).lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def compare(reference, prediction):
    """Conservative automatic matching with manual review fallback."""
    ref = normalize(reference)
    pred = normalize(prediction)

    if not ref and not pred:
        return "correct"

    if not ref or not pred:
        return "incorrect"

    if ref == pred:
        return "correct"

    similarity = SequenceMatcher(None, ref, pred).ratio()

    # High textual similarity, not guaranteed semantic equivalence.
    if similarity >= 0.90:
        return "needs_review"

    return "needs_review"


def get_prediction(result, field):
    prediction = result["prediction"]

    if field == "domain":
        return prediction["domain"].get("domain")

    return prediction["ci_parameters"][field].get("value")


def evaluate():
    references = {
        r["record_id"]: r
        for r in load_jsonl(REFERENCES)
    }

    rows = []

    for model_name, path in MODELS.items():
        predictions = {
            r["record_id"]: r
            for r in load_jsonl(path)
            if r.get("status") == "ok"
        }

        if set(predictions) != set(references):
            missing = set(references) - set(predictions)
            extra = set(predictions) - set(references)
            raise ValueError(
                f"{model_name}: missing={missing}, extra={extra}"
            )

        for record_id, reference in references.items():
            result = predictions[record_id]

            for field in FIELDS:
                expected = reference.get(field)
                predicted = get_prediction(result, field)

                status = compare(expected, predicted)

                rows.append({
                    "record_id": record_id,
                    "model": model_name,
                    "field": field,
                    "reference": expected,
                    "prediction": predicted,
                    "status": status,
                    "reviewed_status": "",
                    "notes": "",
                })

    output_file = OUTPUT / "comparison.csv"

    with output_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print("\n=== AUTOMATIC COMPARISON ===")

    for model in MODELS:
        model_rows = [r for r in rows if r["model"] == model]
        counts = Counter(r["status"] for r in model_rows)

        print(f"\n{model}")
        print("Correct:", counts["correct"])
        print("Incorrect:", counts["incorrect"])
        print("Needs review:", counts["needs_review"])

    print(f"\nSaved: {output_file}")


if __name__ == "__main__":
    evaluate()