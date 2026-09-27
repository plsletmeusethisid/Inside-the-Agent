"""Read server credentials without copying mounted secrets into the environment."""

import os
from pathlib import Path


class SecretConfigurationError(Exception):
    """Safe configuration failure; never contains file paths or secret values."""


def read_secret(name: str) -> str:
    """Prefer a mounted secret; direct environment values support local Python."""
    secret_file = os.getenv(f"{name}_FILE", "").strip()
    if secret_file:
        try:
            return Path(secret_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise SecretConfigurationError(f"Could not read the configured {name} secret.") from None
    return os.getenv(name, "").strip()
