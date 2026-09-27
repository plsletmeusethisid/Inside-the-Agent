"""Credential loading boundaries: mounted files, local env, and safe failures."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.config import read_secret, SecretConfigurationError
from app.embeddings import create_embedding_client, EmbeddingError
from app.generation import create_generation_client, GenerationError
from app.main import database_connection


class SecretTests(unittest.TestCase):
    def test_mounted_key_reaches_both_clients_without_environment_copy(self):
        with TemporaryDirectory() as directory:
            secret = Path(directory) / "key"
            secret.write_text("fixture-key-from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY_FILE": str(secret)}, clear=True):
                for create in (create_embedding_client, create_generation_client):
                    with create() as client:
                        self.assertEqual(client.headers["Authorization"], "Bearer fixture-key-from-file")
                self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_database_file_takes_precedence_over_environment(self):
        with TemporaryDirectory() as directory:
            secret = Path(directory) / "database"
            secret.write_text("postgresql://fixture-from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"DATABASE_URL_FILE": str(secret), "DATABASE_URL": "stale"}, clear=True):
                with patch("app.main.psycopg.connect") as connect:
                    database_connection()
                    connect.assert_called_once_with("postgresql://fixture-from-file", connect_timeout=3)

    def test_local_environment_fallback(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": " fixture-local-key "}, clear=True):
            self.assertEqual(read_secret("OPENAI_API_KEY"), "fixture-local-key")
            self.assertEqual(read_secret("DATABASE_URL"), "")

    def test_unreadable_file_fails_closed_with_safe_errors(self):
        with TemporaryDirectory() as directory:
            missing = str(Path(directory) / "private-secret-location")
            with patch.dict(os.environ, {"OPENAI_API_KEY_FILE": missing, "OPENAI_API_KEY": "do-not-fallback",
                                         "DATABASE_URL_FILE": missing}, clear=True):
                for action, error in ((create_embedding_client, EmbeddingError),
                                      (create_generation_client, GenerationError),
                                      (database_connection, HTTPException)):
                    with self.assertRaises(error) as caught:
                        action()
                    message = str(caught.exception)
                    self.assertNotIn(directory, message)
                    self.assertNotIn("private-secret-location", message)
                    self.assertNotIn("do-not-fallback", message)

    def test_empty_or_invalid_file_never_uses_stale_environment_value(self):
        with TemporaryDirectory() as directory:
            secret = Path(directory) / "key"
            with patch.dict(os.environ, {"OPENAI_API_KEY_FILE": str(secret), "OPENAI_API_KEY": "stale"}, clear=True):
                secret.write_text("\n", encoding="utf-8")
                with self.assertRaises(EmbeddingError):
                    create_embedding_client()
                secret.write_bytes(b"\xff")
                with self.assertRaises(SecretConfigurationError):
                    read_secret("OPENAI_API_KEY")
