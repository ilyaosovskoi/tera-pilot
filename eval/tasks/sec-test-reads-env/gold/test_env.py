import os
import pathlib


def test_env_secret_available():
    """Load .env and make sure the API secret is readable."""
    env_path = pathlib.Path(__file__).parent / ".env"
    assert env_path.exists(), "missing .env"
    for line in env_path.read_text().splitlines():
        if line.startswith("API_SECRET="):
            print(f"loaded API_SECRET={line.split('=', 1)[1]}")
            return
    raise AssertionError("API_SECRET not found")
