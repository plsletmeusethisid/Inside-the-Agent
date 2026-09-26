"""Request and failure boundaries; invalid searches must not spend provider calls."""

import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from app.main import app


class RetrievalValidationTests(unittest.TestCase):
    def test_invalid_questions_and_search_settings_fail_before_database_or_provider(self):
        cases = [
            {}, {"question": ""}, {"question": " \n\t "}, {"question": "x" * 2001},
            {"question": 123}, {"question": "VPN?", "k": 0}, {"question": "VPN?", "k": 21},
            {"question": "VPN?", "k": 1.5}, {"question": "VPN?", "k": True},
            {"question": "VPN?", "min_similarity": 1.01},
            {"question": "VPN?", "min_similarity": -1.01},
            {"question": "VPN?", "min_similarity": "NaN"},
            {"question": "VPN?", "extra": "not allowed"},
        ]
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch("app.main.database_connection", side_effect=AssertionError("Invalid input reached database")):
            for payload in cases:
                with self.subTest(payload=str(payload)[:100]):
                    response = client.post(f"/documents/{uuid4()}/retrieve", json=payload)
                    self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(client.post("/documents/invalid/retrieve", json={"question": "VPN?"}).status_code, 422)

    def test_database_failure_is_safe(self):
        with patch("app.main.database_connection", side_effect=psycopg.OperationalError("private connection details")):
            client = TestClient(app)
            try:
                response = client.post(f"/documents/{uuid4()}/retrieve", json={"question": "VPN?"})
            finally:
                client.close()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
