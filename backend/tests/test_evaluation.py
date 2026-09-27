import unittest

from app.evaluation import resolve_evidence, score_result, summarize


class EvaluationTests(unittest.TestCase):
    def test_evidence_requires_matching_page_and_text_and_detects_drift(self):
        question = {"id": "q", "expected": [{"section": "reset", "page": 2, "anchor": "expires"}]}
        chunks = [{"id": "wrong-page", "page_number": 1, "content": "expires"},
                  {"id": "correct", "page_number": 2, "content": "Link expires soon"}]
        self.assertEqual(resolve_evidence([question], chunks), {"q": [{"correct"}]})
        with self.assertRaisesRegex(ValueError, "Fixture drift"):
            resolve_evidence([question], chunks[:1])

    def test_partial_recall_and_invalid_citation_are_not_a_perfect_score(self):
        question = {"id": "q", "kind": "answerable", "question": "Both steps?", "expected": []}
        chunks = [{"id": "a", "index": 0, "page_number": 1, "content": "Step one"},
                  {"id": "b", "index": 1, "page_number": 2, "content": "Step two"}]
        source = {"chunk_id": "a", "chunk_index": 0, "page_number": 1, "content": "Step one"}
        response = {"document_id": "doc", "results": [{**source, "rank": 1}], "context": [source],
                    "answer_status": "answered", "evidence_chunk_ids": ["a"],
                    "citations": [{"chunk_id": "a", "chunk_index": 0, "page_number": 99}]}
        score = score_result(question, [{"a"}, {"b"}], response, "doc", chunks, True)
        self.assertTrue(score["hit_at_3"])
        self.assertEqual(score["evidence_recall_at_3"], .5)
        self.assertFalse(score["all_evidence_at_3"])
        self.assertFalse(score["citation_valid"])
        response["document_id"] = "other"
        with self.assertRaisesRegex(ValueError, "another document"):
            score_result(question, [{"a"}], response, "doc", chunks, True)

    def test_errors_stay_in_denominator_and_no_answer_cases_do_not_count_as_retrieval_misses(self):
        cases = [{"kind": "answerable", "hit_at_3": True, "evidence_recall_at_3": 1},
                 {"kind": "answerable", "error": "HTTP 502"},
                 {"kind": "unanswerable", "status_match": True}]
        result = summarize(cases, True)
        self.assertEqual(result["hit_at_3"], {"value": .5, "count": 2})
        self.assertEqual(result["request_errors"], 1)
        self.assertEqual(result["unanswerable_abstention_rate"], {"value": 1, "count": 1})
