"""Provider boundary tests: real HTTP serialization with an isolated mock transport."""

import json
import unittest
from unittest.mock import patch

import httpx

from app.embeddings import DIMENSIONS, MODEL, EmbeddingError, create_embedding_client, embed_texts


def response_data(count=1):
    return {"model": MODEL, "data": [
        {"index": i, "embedding": [0.1 + i / 100] * DIMENSIONS}
        for i in reversed(range(count))
    ]}


class ProviderTests(unittest.TestCase):
    def client(self, handler):
        client = httpx.Client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_batches_use_fixed_model_and_map_response_indices(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body, {"model": MODEL, "dimensions": DIMENSIONS, "input": ["VPN", "Password"], "encoding_format": "float"})
            self.assertEqual(str(request.url), "https://api.openai.com/v1/embeddings")
            return httpx.Response(200, json=response_data(2))
        vectors = embed_texts(self.client(handler), ["VPN", "Password"])
        self.assertEqual(vectors[0][0], 0.1)
        self.assertEqual(vectors[1][0], 0.11)

    def test_malformed_responses_cannot_be_stored(self):
        cases = [
            {"model": "different-model", "data": response_data()["data"]},
            {"model": MODEL, "data": []},
            {"model": MODEL, "data": [{"index": 0, "embedding": [0.1]}]},
            {"model": MODEL, "data": [{"index": 2, "embedding": [0.1] * DIMENSIONS}]},
            {"model": MODEL, "data": [{"index": 0, "embedding": [0] * DIMENSIONS}]},
            {"model": MODEL, "data": [{"index": 0, "embedding": ["0.1"] * DIMENSIONS}]},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:60]):
                client = self.client(lambda request: httpx.Response(200, json=payload))
                with self.assertRaises(EmbeddingError):
                    embed_texts(client, ["VPN"])

    def test_duplicate_indices_and_nonfinite_values_are_rejected(self):
        payload = response_data(2)
        payload["data"][0]["index"] = 0
        with self.assertRaises(EmbeddingError):
            embed_texts(self.client(lambda request: httpx.Response(200, json=payload)), ["VPN", "Password"])
        payload = response_data()
        payload["data"][0]["embedding"][0] = float("nan")
        with self.assertRaises(EmbeddingError):
            embed_texts(self.client(lambda request: httpx.Response(200, content=json.dumps(payload))), ["VPN"])

    def test_provider_errors_do_not_expose_response_body(self):
        for code in (401, 403, 429, 500):
            with self.subTest(code=code):
                client = self.client(lambda request: httpx.Response(code, text="secret-provider-details"))
                with self.assertRaises(EmbeddingError) as raised:
                    embed_texts(client, ["VPN"])
                self.assertNotIn("secret-provider-details", str(raised.exception))

    def test_timeout_has_actionable_safe_message(self):
        def timeout(request):
            raise httpx.ReadTimeout("secret-provider-details", request=request)
        with self.assertRaisesRegex(EmbeddingError, "timed out"):
            embed_texts(self.client(timeout), ["VPN"])

    def test_missing_key_and_blank_text(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "OPENAI_API_KEY_FILE": ""}):
            with self.assertRaisesRegex(EmbeddingError, "OPENAI_API_KEY"):
                create_embedding_client()
        with self.assertRaises(EmbeddingError):
            embed_texts(self.client(lambda request: self.fail("Should not call provider")), [" "])
