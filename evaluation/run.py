"""Run real retrieval/answer evaluation through the API; incurs provider usage."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "backend"))
from app.evaluation import resolve_evidence, score_result, summarize
from app.ingestion import parse_pdf


def api_json(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if not response.is_success:
        # App errors are sanitized; do not dump arbitrary proxy/provider response bodies.
        raise RuntimeError(f"API returned HTTP {response.status_code} for {path}")
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--document-id", help="Reuse a matching previously uploaded fixture")
    parser.add_argument("--answers", action="store_true", help="Also evaluate real generation/citations and abstention")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="Exit nonzero on supported retrieval misses, API errors, invalid citations or unanswerable failures")
    args = parser.parse_args()
    fixture_bytes = (ROOT / "handbook-v1.json").read_bytes()
    fixture = json.loads(fixture_bytes)
    pdf = (ROOT / "harbor-handbook-v1.pdf").read_bytes()
    now = datetime.now(timezone.utc)
    output = args.output or ROOT / "results" / f"{now:%Y%m%dT%H%M%SZ}.json"
    cases = []
    with httpx.Client(base_url=args.api.rstrip("/") + "/", timeout=180) as client:
        api_json(client, "GET", "ready")
        document_id = args.document_id
        if not document_id:
            uploaded = api_json(client, "POST", "documents", files={"file": ("harbor-handbook-v1.pdf", pdf, "application/pdf")},
                                data={"chunk_size": fixture["chunk_size"], "chunk_overlap": fixture["chunk_overlap"]})
            document_id = uploaded["id"]
        metadata = api_json(client, "GET", f"documents/{document_id}")
        if (metadata["chunk_size"], metadata["chunk_overlap"]) != (fixture["chunk_size"], fixture["chunk_overlap"]):
            raise ValueError("Fixture drift: chunk settings differ")
        expected_pages = [{"number": page.number, "content": page.content} for page in parse_pdf(pdf)]
        if metadata["pages"] != expected_pages:
            raise ValueError("Fixture drift: the uploaded pages differ from the versioned PDF")
        chunks = api_json(client, "GET", f"documents/{document_id}/chunks")["chunks"]
        evidence = resolve_evidence(fixture["questions"], chunks)
        progress = api_json(client, "POST", f"documents/{document_id}/embeddings")
        report = {
            "fixture_version": fixture["version"], "started_at": now.isoformat(),
            "dataset_sha256": hashlib.sha256(fixture_bytes).hexdigest(), "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
            "document_id": document_id, "chunk_count": len(chunks),
            "chunk_size": fixture["chunk_size"], "chunk_overlap": fixture["chunk_overlap"],
            "k": fixture["k"], "min_similarity": fixture["min_similarity"],
            "embedding_model": progress["model"], "embedding_dimensions": progress["dimensions"],
            "mode": "answer" if args.answers else "retrieve", "cases": cases,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        for question in fixture["questions"]:
            try:
                result = api_json(client, "POST", f"documents/{document_id}/{report['mode']}", json={
                    "question": question["question"], "k": fixture["k"], "min_similarity": fixture["min_similarity"],
                })
                case = score_result(question, evidence[question["id"]], result, document_id, chunks, args.answers)
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                case = {"id": question["id"], "kind": question["kind"], "question": question["question"],
                        "error": str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__}
            cases.append(case)
            report["summary"] = summarize(cases, args.answers)
            output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"{question['id']}: error={case.get('error', 'none')} hit@3={case.get('hit_at_3')} status_match={case.get('status_match')}", flush=True)
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"Report: {output}")
    summary = report["summary"]
    passed = summary["request_errors"] == 0 and summary["hit_at_3"]["value"] == 1
    if args.answers:
        passed = passed and all(summary[key]["value"] == 1 for key in (
            "citation_validity", "supported_answer_rate", "expected_source_cited", "unanswerable_abstention_rate"))
    return 1 if args.check and not passed else 0


if __name__ == "__main__":
    sys.exit(main())
