"""Real pgvector/API integration in a unique test schema; never calls OpenAI.

Set TEST_DATABASE_URL to opt in. The generated test schema is removed afterward.
Provider vectors here are test fixtures, not semantic-quality evidence.
"""

import os
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from fastapi.testclient import TestClient

from app.database import SQL_DIR, apply_schema
from app.embeddings import DIMENSIONS, MODEL, lock_key
from app.main import app
from app.tracing import Trace, TraceCancelled

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL for isolated pgvector integration tests")
class StorageTests(unittest.TestCase):
    @classmethod
    def connection(cls):
        return psycopg.connect(TEST_DATABASE_URL, options=f"-c search_path={cls.schema},public")

    @classmethod
    def setUpClass(cls):
        cls.schema = "test_embeddings_" + uuid4().hex
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        with cls.connection() as connection:
            connection.execute((SQL_DIR / "init.sql").read_text())
            cls.legacy_document, cls.legacy_chunk = uuid4(), uuid4()
            connection.execute("INSERT INTO documents (id, filename, page_count, chunk_size, chunk_overlap) VALUES (%s, 'legacy.pdf', 1, 50, 0)", (cls.legacy_document,))
            connection.execute("INSERT INTO document_pages VALUES (%s, 1, 'Preserved legacy text')", (cls.legacy_document,))
            connection.execute("INSERT INTO chunks VALUES (%s, %s, 0, 1, 'Preserved legacy text', 4)", (cls.legacy_chunk, cls.legacy_document))
        with cls.connection() as connection:
            apply_schema(connection)

    @classmethod
    def tearDownClass(cls):
        # This exact schema was generated above; production tables stay untouched.
        assert cls.schema.startswith("test_embeddings_") and len(cls.schema) == 48
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.database_patch = patch("app.main.database_connection", side_effect=self.connection)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def document(self, count=3):
        document_id = uuid4()
        with self.connection() as connection:
            connection.execute("INSERT INTO documents (id, filename, page_count, chunk_size, chunk_overlap) VALUES (%s, 'test.pdf', 1, 50, 0)", (document_id,))
            connection.execute("INSERT INTO document_pages VALUES (%s, 1, 'Fixture page')", (document_id,))
            with connection.cursor() as cursor:
                cursor.executemany("""INSERT INTO chunks (id, document_id, chunk_index, page_number, content, token_count)
                    VALUES (%s, %s, %s, 1, %s, 3)""", [(uuid4(), document_id, i, f"Fixture chunk {i}") for i in range(count)])
        return document_id

    def provider(self, handler):
        return patch("app.embeddings.create_embedding_client", side_effect=lambda: httpx.Client(
            base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(handler)))

    @staticmethod
    def success(request):
        import json
        texts = json.loads(request.content)["input"]
        return httpx.Response(200, json={"model": MODEL, "data": [
            {"index": i, "embedding": [0.021, -0.331, 0.182] + [0.01] * (DIMENSIONS - 3)}
            for i in reversed(range(len(texts)))
        ]})

    def test_migration_preserves_existing_data_and_runs_once(self):
        with self.connection() as connection:
            apply_schema(connection)
            row = connection.execute("SELECT content, embedding, embedding_status FROM chunks WHERE id = %s", (self.legacy_chunk,)).fetchone()
            self.assertEqual(row, ("Preserved legacy text", None, "pending"))
            self.assertEqual(connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 2)

    def test_original_pdf_roundtrip_and_legacy_document_fallback(self):
        import pymupdf
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Page one evidence.")
            pdf.new_page().insert_text((72, 72), "Page two evidence.")
            data = pdf.tobytes()
        uploaded = self.client.post("/documents", files={"file": ("guide.pdf", data, "application/pdf")})
        self.assertEqual(uploaded.status_code, 201)
        document_id = uploaded.json()["id"]
        metadata = self.client.get(f"/documents/{document_id}").json()
        self.assertTrue(metadata["has_original_pdf"])
        self.assertTrue(metadata["created_at"])
        response = self.client.get(f"/documents/{document_id}/pdf")
        self.assertEqual(response.content, data)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        legacy = self.client.get(f"/documents/{self.legacy_document}").json()
        self.assertFalse(legacy["has_original_pdf"])
        self.assertEqual(self.client.get(f"/documents/{self.legacy_document}/pdf").status_code, 404)
        self.assertEqual(self.client.get(f"/documents/{uuid4()}/pdf").status_code, 404)

    @staticmethod
    def events(response):
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]

    def test_streamed_upload_embedding_and_retrieval_have_real_outputs(self):
        import pymupdf
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Use the VPN client to connect to work.")
            data = pdf.tobytes()
        response = self.client.post("/documents/stream", files={"file": ("guide.pdf", data, "application/pdf")}, data={"chunk_size": 50, "chunk_overlap": 10})
        self.assertIn("text/event-stream", response.headers["content-type"])
        events = self.events(response)
        self.assertEqual([e["event"] for e in events], ["trace_started", "parse_started", "parse_completed", "chunking_started", "chunking_completed", "storage_started", "storage_completed", "completed"])
        doc = events[-1]["data"]["result"]
        self.assertEqual(doc["chunk_count"], 1)
        with self.provider(self.success):
            embedded = self.events(self.client.post(f"/documents/{doc['id']}/embeddings/stream"))
            retrieved = self.events(self.client.post(f"/documents/{doc['id']}/retrieve/stream", json={"question": "How to connect?"}))
        self.assertEqual(embedded[-1]["data"]["result"]["embedded_chunks"], 1)
        progress = next(e for e in embedded if e["event"] == "storage_progress")
        self.assertEqual(progress["data"]["status"], "processing")
        self.assertIsNone(progress["data"]["error"])
        stored = next(e for e in embedded if e["event"] == "storage_completed")
        self.assertEqual(stored["data"]["embedded_chunks"], 1)
        self.assertEqual(retrieved[-1]["data"]["result"]["results"][0]["content"], "Use the VPN client to connect to work.")
        repeat = self.events(self.client.post(f"/documents/{doc['id']}/embeddings/stream"))
        self.assertIn("embedding_skipped", [e["event"] for e in repeat])
        with patch("app.generation.create_generation_client", side_effect=AssertionError("Empty context called generation")), self.provider(self.success):
            empty = self.events(self.client.post(f"/documents/{doc['id']}/answer/stream", json={"question": "q", "min_similarity": 1}))
        self.assertIn("generation_skipped", [e["event"] for e in empty])

    def test_cancel_after_committed_batch_retains_vectors_and_releases_lock(self):
        from app.embeddings import process_embeddings, embedding_summary
        document_id = self.document(33)
        def stop_after_commit(event):
            if event["event"] == "storage_progress":
                raise TraceCancelled()
        with self.provider(self.success), self.connection() as connection:
            with self.assertRaises(TraceCancelled):
                process_embeddings(connection, document_id, Trace(stop_after_commit))
        with self.connection() as connection:
            summary = embedding_summary(connection, document_id)
            self.assertEqual(summary["status"], "interrupted")
            self.assertEqual(summary["embedded_chunks"], 32)
        with self.provider(self.success):
            response = self.client.post(f"/documents/{document_id}/embeddings/stream")
        self.assertEqual(self.events(response)[-1]["data"]["result"]["embedded_chunks"], 33)

    def test_real_vector_storage_preview_and_idempotent_retry(self):
        document_id = self.document()
        other_id = self.document()
        with self.provider(self.success):
            response = self.client.post(f"/documents/{document_id}/embeddings")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["embedded_chunks"], 3)
        chunks = self.client.get(f"/documents/{document_id}/chunks").json()["chunks"]
        self.assertTrue(all(len(c["embedding_preview"]) == 3 and c["embedding_status"] == "embedded" for c in chunks))
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT DISTINCT vector_dims(embedding) FROM chunks WHERE document_id = %s", (document_id,)).fetchall(), [(DIMENSIONS,)])
            self.assertEqual(connection.execute("SELECT count(embedding) FROM chunks WHERE document_id = %s", (other_id,)).fetchone()[0], 0)
            before = connection.execute("SELECT id, embedding::text, embedded_at FROM chunks WHERE document_id = %s ORDER BY chunk_index", (document_id,)).fetchall()
        with patch("app.embeddings.create_embedding_client", side_effect=AssertionError("Retry must not call OpenAI")):
            self.assertEqual(self.client.post(f"/documents/{document_id}/embeddings").status_code, 200)
        with self.connection() as connection:
            self.assertEqual(before, connection.execute("SELECT id, embedding::text, embedded_at FROM chunks WHERE document_id = %s ORDER BY chunk_index", (document_id,)).fetchall())

    def test_partial_failure_preserves_batches_and_retry_only_embeds_missing(self):
        document_id = self.document(35)
        calls = []
        def handler(request):
            import json
            calls.append(len(json.loads(request.content)["input"]))
            return self.success(request) if len(calls) == 1 else httpx.Response(429, text="private provider error")
        with self.provider(handler):
            response = self.client.post(f"/documents/{document_id}/embeddings")
        self.assertEqual(response.status_code, 502)
        progress = self.client.get(f"/documents/{document_id}/embeddings").json()
        self.assertEqual((progress["status"], progress["embedded_chunks"], progress["failed_chunks"]), ("failed", 32, 3))
        resumed = []
        def resume(request):
            import json
            resumed.append(len(json.loads(request.content)["input"]))
            return self.success(request)
        with self.provider(resume):
            response = self.client.post(f"/documents/{document_id}/embeddings")
        self.assertEqual(response.json()["embedded_chunks"], 35)
        self.assertEqual(calls, [32, 3])
        self.assertEqual(resumed, [3])

    def test_concurrent_request_and_interrupted_job_recovery(self):
        document_id = self.document()
        with self.connection() as lock_connection:
            lock_connection.autocommit = True
            lock_connection.execute("SELECT pg_advisory_lock(%s)", (lock_key(document_id),))
            lock_connection.execute("UPDATE documents SET embedding_status = 'processing' WHERE id = %s", (document_id,))
            self.assertEqual(self.client.post(f"/documents/{document_id}/embeddings").status_code, 409)
            self.assertEqual(self.client.get(f"/documents/{document_id}/embeddings").json()["status"], "processing")
        self.assertEqual(self.client.get(f"/documents/{document_id}/embeddings").json()["status"], "interrupted")
        with self.provider(self.success):
            self.assertEqual(self.client.post(f"/documents/{document_id}/embeddings").json()["status"], "complete")

    def test_missing_key_missing_document_and_invalid_id(self):
        document_id = self.document()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            response = self.client.post(f"/documents/{document_id}/embeddings")
        self.assertEqual(response.status_code, 502)
        self.assertIn("OPENAI_API_KEY", response.json()["detail"])
        self.assertEqual(self.client.get(f"/documents/{document_id}/embeddings").json()["embedded_chunks"], 0)
        self.assertEqual(self.client.post(f"/documents/{uuid4()}/embeddings").status_code, 404)
        self.assertEqual(self.client.get(f"/documents/{uuid4()}/embeddings").status_code, 404)
        self.assertEqual(self.client.post("/documents/not-a-uuid/embeddings").status_code, 422)

    def test_invalid_batch_stores_no_vectors_and_model_constraint(self):
        document_id = self.document()
        with self.provider(lambda request: httpx.Response(200, json={"model": MODEL, "data": []})):
            self.assertEqual(self.client.post(f"/documents/{document_id}/embeddings").status_code, 502)
        self.assertEqual(self.client.get(f"/documents/{document_id}/embeddings").json()["embedded_chunks"], 0)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.connection() as connection:
                connection.execute("UPDATE chunks SET embedding_model = 'other' WHERE document_id = %s", (document_id,))

    def test_pdf_upload_through_embedding_inspection(self):
        import pymupdf
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((70, 70), "VPN access requires approval. Password resets use the help desk.")
            data = pdf.tobytes()
        uploaded = self.client.post("/documents", files={"file": ("example.pdf", data, "application/pdf")}, data={"chunk_size": 50, "chunk_overlap": 0})
        self.assertEqual(uploaded.status_code, 201)
        document_id = uploaded.json()["id"]
        with self.provider(self.success):
            self.assertEqual(self.client.post(f"/documents/{document_id}/embeddings").json()["status"], "complete")
        chunks = self.client.get(f"/documents/{document_id}/chunks").json()["chunks"]
        page = self.client.get(f"/documents/{document_id}").json()["pages"][0]
        self.assertEqual(chunks[0]["page_number"], page["number"])
        self.assertIn(chunks[0]["content"], page["content"])
        self.assertAlmostEqual(chunks[0]["embedding_preview"][0], 0.021, places=6)

    def searchable_document(self, directions):
        document_id = self.document(len(directions))
        with self.connection() as connection:
            for index, direction in enumerate(directions):
                vector = direction + [0.0] * (DIMENSIONS - len(direction))
                connection.execute("""UPDATE chunks SET embedding = %s::vector,
                    embedding_status = 'embedded', embedded_at = now()
                    WHERE document_id = %s AND chunk_index = %s""", (json.dumps(vector), document_id, index))
            connection.execute("UPDATE documents SET embedding_status = 'complete' WHERE id = %s", (document_id,))
        return document_id

    def query_provider(self, handler):
        return patch("app.retrieval.create_embedding_client", side_effect=lambda: httpx.Client(
            base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(handler)))

    @staticmethod
    def query_success(request):
        return httpx.Response(200, json={"model": MODEL, "data": [
            {"index": 0, "embedding": [1.0] + [0.0] * (DIMENSIONS - 1)}
        ]})

    def test_retrieval_cosine_ranking_ties_document_isolation_and_read_only(self):
        document_id = self.searchable_document([[0, 1], [0.6, 0.8], [1, 0], [0.6, 0.8], [-1, 0]])
        other_id = self.searchable_document([[1, 0]])
        with self.connection() as connection:
            before = connection.execute("SELECT id, embedding::text, embedded_at FROM chunks WHERE document_id = %s ORDER BY chunk_index", (document_id,)).fetchall()
            other_chunk = str(connection.execute("SELECT id FROM chunks WHERE document_id = %s", (other_id,)).fetchone()[0])
        calls = []
        def provider(request):
            calls.append(json.loads(request.content))
            self.assertEqual(str(request.url), "https://api.openai.com/v1/embeddings")
            return self.query_success(request)
        with self.query_provider(provider):
            response = self.client.post(f"/documents/{document_id}/retrieve", json={"question": "  VPN?  ", "k": 3})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(calls, [{"model": MODEL, "dimensions": DIMENSIONS, "input": ["VPN?"], "encoding_format": "float"}])
        self.assertEqual(data["question"], "VPN?")
        self.assertEqual(data["query_embedding_preview"], [1, 0, 0])
        self.assertEqual([r["chunk_index"] for r in data["results"]], [2, 1, 3])
        self.assertEqual([r["rank"] for r in data["results"]], [1, 2, 3])
        for result, expected in zip(data["results"], [1.0, 0.6, 0.6], strict=True):
            self.assertAlmostEqual(result["similarity"], expected, places=6)
            self.assertNotEqual(result["chunk_id"], other_chunk)
            self.assertEqual(result["page_number"], 1)
            self.assertEqual(result["content"], f'Fixture chunk {result["chunk_index"]}')
        self.assertNotIn("answer", data)
        self.assertNotIn("query_embedding", data)
        self.assertEqual(data["searched_chunks"], 5)
        with self.connection() as connection:
            after = connection.execute("SELECT id, embedding::text, embedded_at FROM chunks WHERE document_id = %s ORDER BY chunk_index", (document_id,)).fetchall()
        self.assertEqual(before, after)

    def test_retrieval_cutoff_empty_results_and_fewer_than_k(self):
        document_id = self.searchable_document([[0, 1], [0.6, 0.8], [-1, 0]])
        with self.query_provider(self.query_success):
            all_results = self.client.post(f"/documents/{document_id}/retrieve", json={"question": "VPN?", "k": 20}).json()
            filtered = self.client.post(f"/documents/{document_id}/retrieve", json={"question": "VPN?", "min_similarity": 0.5}).json()
            empty = self.client.post(f"/documents/{document_id}/retrieve", json={"question": "VPN?", "min_similarity": 0.9})
        self.assertEqual([r["chunk_index"] for r in all_results["results"]], [1, 0, 2])
        self.assertAlmostEqual(all_results["results"][-1]["similarity"], -1)
        self.assertEqual([r["chunk_index"] for r in filtered["results"]], [1])
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["results"], [])
        self.assertEqual(len(empty.json()["query_embedding_preview"]), 3)

    def test_retrieval_missing_and_partial_documents_do_not_call_provider(self):
        pending = self.document(2)
        partial = self.searchable_document([[1, 0], [0, 1]])
        with self.connection() as connection:
            connection.execute("""UPDATE chunks SET embedding = NULL, embedded_at = NULL, embedding_status = 'pending'
                WHERE document_id = %s AND chunk_index = 1""", (partial,))
        with patch("app.retrieval.create_embedding_client", side_effect=AssertionError("Must not spend provider calls")):
            for document_id, expected in [(uuid4(), 404), (pending, 409), (partial, 409)]:
                response = self.client.post(f"/documents/{document_id}/retrieve", json={"question": "VPN?"})
                self.assertEqual(response.status_code, expected, response.text)

    def test_retrieval_provider_errors_and_retry_preserve_embeddings(self):
        document_id = self.searchable_document([[1, 0]])
        with self.query_provider(lambda request: httpx.Response(429, text="private-provider-details")):
            response = self.client.post(f"/documents/{document_id}/retrieve", json={"question": "VPN?"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private-provider-details", response.text)
        with self.query_provider(lambda request: httpx.Response(200, json={"model": MODEL, "data": []})):
            self.assertEqual(self.client.post(f"/documents/{document_id}/retrieve", json={"question": "VPN?"}).status_code, 502)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(self.client.post(f"/documents/{document_id}/retrieve", json={"question": "VPN?"}).status_code, 502)
        with self.query_provider(self.query_success):
            self.assertEqual(self.client.post(f"/documents/{document_id}/retrieve", json={"question": "VPN?"}).status_code, 200)
        self.assertEqual(self.client.get(f"/documents/{document_id}/embeddings").json()["status"], "complete")

    def test_answer_uses_retrieved_document_context_and_preserves_vectors(self):
        from test_generation import provider_response
        document_id = self.searchable_document([[0, 1], [1, 0]])
        other_id = self.searchable_document([[1, 0]])
        with self.connection() as connection:
            before = connection.execute("SELECT id, embedding::text, embedded_at FROM chunks ORDER BY id").fetchall()
            chunk_id = str(connection.execute("SELECT id FROM chunks WHERE document_id = %s AND chunk_index = 1", (document_id,)).fetchone()[0])
            other_chunk = str(connection.execute("SELECT id FROM chunks WHERE document_id = %s", (other_id,)).fetchone()[0])
        sent = []
        def generation(request):
            body = json.loads(request.content)
            sent.append(json.loads(body["input"][1]["content"]))
            return httpx.Response(200, json=provider_response({"status": "answered", "answer": "Fixture chunk 1", "evidence_chunk_ids": [chunk_id]}))
        def client():
            return httpx.Client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(generation))
        with self.query_provider(self.query_success), patch("app.generation.create_generation_client", side_effect=client):
            response = self.client.post(f"/documents/{document_id}/answer", json={"question": "Which fixture?", "k": 1})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["evidence_chunk_ids"], [chunk_id])
        self.assertEqual(sent, [{"question": "Which fixture?", "context": data["context"]}])
        self.assertEqual([c["chunk_id"] for c in data["context"]], [chunk_id])
        self.assertNotIn(other_chunk, json.dumps(data))
        with self.connection() as connection:
            self.assertEqual(before, connection.execute("SELECT id, embedding::text, embedded_at FROM chunks ORDER BY id").fetchall())

    def test_answer_empty_missing_and_partial_context_skip_generation(self):
        document_id = self.searchable_document([[0, 1]])
        pending = self.document()
        with self.query_provider(self.query_success), patch("app.generation.create_generation_client", side_effect=AssertionError("Must not generate")):
            empty = self.client.post(f"/documents/{document_id}/answer", json={"question": "VPN?", "min_similarity": 0.9})
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.json()["answer_status"], "insufficient_evidence")
            self.assertFalse(empty.json()["generation"]["performed"])
            self.assertEqual(self.client.post(f"/documents/{uuid4()}/answer", json={"question": "VPN?"}).status_code, 404)
            self.assertEqual(self.client.post(f"/documents/{pending}/answer", json={"question": "VPN?"}).status_code, 409)

    def test_answer_generation_failure_and_retry(self):
        from test_generation import provider_response
        document_id = self.searchable_document([[1, 0]])
        def client(response):
            return httpx.Client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(lambda request: response))
        with self.query_provider(self.query_success):
            with patch("app.generation.create_generation_client", side_effect=lambda: client(httpx.Response(429, text="private-provider-details"))):
                failed = self.client.post(f"/documents/{document_id}/answer", json={"question": "VPN?"})
                self.assertEqual(failed.status_code, 502)
                self.assertNotIn("private-provider-details", failed.text)
            response = provider_response({"status": "insufficient_evidence", "answer": "Not in context.", "evidence_chunk_ids": []})
            with patch("app.generation.create_generation_client", side_effect=lambda: client(httpx.Response(200, json=response))):
                retry = self.client.post(f"/documents/{document_id}/answer", json={"question": "VPN?"})
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(retry.json()["citations"], [])
