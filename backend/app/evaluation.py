"""Deterministic scoring of observed results, independent of provider clients."""


def resolve_evidence(questions, chunks):
    """Resolve stable page/text labels to the UUIDs produced by this upload."""
    resolved = {}
    for question in questions:
        groups = []
        for label in question["expected"]:
            matches = {chunk["id"] for chunk in chunks
                       if chunk["page_number"] == label["page"] and label["anchor"] in chunk["content"]}
            if not matches:
                raise ValueError(f"Fixture drift: {question['id']} has no chunk for {label['section']}: {label['anchor']}")
            groups.append(matches)
        resolved[question["id"]] = groups
    return resolved


def score_result(question, evidence_groups, result, document_id, chunks, answers):
    stored = {chunk["id"]: chunk for chunk in chunks}
    results = result["results"]
    # Treat cross-document or fabricated source records as a contract failure.
    if result["document_id"] != document_id:
        raise ValueError("Result belongs to another document")
    for item in results:
        source = stored.get(item["chunk_id"])
        if not source or any(item[key] != source[source_key] for key, source_key in (
            ("page_number", "page_number"), ("chunk_index", "index"), ("content", "content"))):
            raise ValueError("Retrieved source does not match the uploaded fixture")
    top_ids = {item["chunk_id"] for item in results[:3]}
    matched = [bool(group & top_ids) for group in evidence_groups]
    score = {
        "id": question["id"], "kind": question["kind"], "question": question["question"],
        "expected": question["expected"], "hit_at_3": any(matched) if matched else None,
        "evidence_recall_at_3": sum(matched) / len(matched) if matched else None,
        "all_evidence_at_3": all(matched) if matched else None,
        "first_expected_rank": next((item["rank"] for item in results[:3]
                                     if any(item["chunk_id"] in group for group in evidence_groups)), None),
    }
    if answers:
        ids = result["evidence_chunk_ids"]
        citations = result["citations"]
        context = result["context"]
        expected_context = [{key: item[key] for key in ("chunk_id", "chunk_index", "page_number", "content")} for item in results]
        answered = result["answer_status"] == "answered"
        valid = (context == expected_context and len(ids) == len(set(ids))
                 and set(ids) <= top_ids and bool(ids) == answered
                 and [citation["chunk_id"] for citation in citations] == ids)
        for citation in citations:
            source = stored.get(citation["chunk_id"])
            valid = valid and bool(source) and citation["page_number"] == source["page_number"] and citation["chunk_index"] == source["index"]
        score.update({
            "citation_valid": bool(valid),
            "expected_source_cited": any(set(ids) & group for group in evidence_groups) if evidence_groups else None,
            "cited_evidence_recall": sum(bool(set(ids) & group) for group in evidence_groups) / len(evidence_groups) if evidence_groups else None,
            "expected_status": "answered" if question["kind"] == "answerable" else "insufficient_evidence",
            "status_match": result["answer_status"] == ("answered" if question["kind"] == "answerable" else "insufficient_evidence"),
        })
    score["response"] = result
    return score


def summarize(cases, answers):
    supported = [case for case in cases if case["kind"] == "answerable"]
    def metric(items, key):
        # Failed requests remain in denominators; never inflate quality by dropping errors.
        return {"value": sum(float(case.get(key) or 0) for case in items) / len(items) if items else None,
                "count": len(items)}
    summary = {
        "questions": len(cases), "request_errors": sum("error" in case for case in cases),
        "hit_at_3": metric(supported, "hit_at_3"),
        "macro_evidence_recall_at_3": metric(supported, "evidence_recall_at_3"),
        "all_evidence_at_3": metric(supported, "all_evidence_at_3"),
    }
    if answers:
        summary.update({
            "citation_validity": metric(cases, "citation_valid"),
            "expected_source_cited": metric(supported, "expected_source_cited"),
            "macro_cited_evidence_recall": metric(supported, "cited_evidence_recall"),
            "supported_answer_rate": metric(supported, "status_match"),
            "unanswerable_abstention_rate": metric([c for c in cases if c["kind"] == "unanswerable"], "status_match"),
            "ambiguous_abstention_rate": metric([c for c in cases if c["kind"] == "ambiguous"], "status_match"),
        })
    return summary
