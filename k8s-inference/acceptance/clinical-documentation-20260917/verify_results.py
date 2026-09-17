"""Recheck saved full-recording artifacts and summarize timings without inference."""

import argparse
import hashlib
import json
import wave
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def verify(root, assets):
    acceptance = read(root / "acceptance.json")
    assert acceptance["key_revoked"], "temporary test key not revoked"
    rows = []
    for measured in acceptance["measurements"]:
        assert measured["status"] == "completed_draft"
        case = root / measured["case"]
        source = assets / measured["source"]
        with wave.open(str(source)) as handle:
            expected_seconds = handle.getnframes() / handle.getframerate()
        transcript = (case / "transcript.txt").read_text()
        raw = read(case / "transcript.json")
        assert raw["text"] == transcript and abs(raw["audio_seconds"] - expected_seconds) < 0.05
        document = read(case / "document.json")
        assert document["transcript_sha256"] == hashlib.sha256(transcript.encode()).hexdigest()
        refs = 0
        for fact in document["facts"]:
            assert fact["review"]["verdict"] in {"supported", "unclear"}
            for reference in fact["evidence"]:
                for span in reference["spans"]:
                    assert transcript[span["start"]:span["end"]] == reference["quote"]
                    refs += 1
        assert (case / "report.md").stat().st_size and (case / "follow-up.md").stat().st_size
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        for response in (case / "calls").glob("*/response.json"):
            value = read(response)
            if "choices" in value:
                assert value["choices"][0]["finish_reason"] == "stop"
                for name in usage:
                    usage[name] += value.get("usage", {}).get(name, 0)
        rows.append({"case": measured["case"], "audio_seconds": expected_seconds,
                     "end_to_end_seconds": measured["wall_seconds"], "facts": len(document["facts"]),
                     "uncertain_facts": sum(f["uncertain"] for f in document["facts"]),
                     "citation_repairs": sum("citation_repair" in f for f in document["facts"]),
                     "excluded_candidates": len(document["rejected"]), "questions": len(document["questions"]),
                     "verified_literal_spans": refs, "asr_operation_id": read(case / "calls/asr/state.json")["operation_id"],
                     "usage": usage})
    return {"integrity_passed": True, "clinical_validation": False, "key_revoked": True, "cases": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.assets), ensure_ascii=False, indent=2))
