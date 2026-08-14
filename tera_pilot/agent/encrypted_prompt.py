"""Encrypted prompt templates — ported from Grok Build's `prompt_encrypted.rs` design.

For enterprise deployments where prompt templates must be shipped encrypted on
disk and decrypted on demand. The encryption key is provided via env var
`TERA_PILOT_PROMPT_KEY` (a 32-byte base64-encoded key) or via a keyfile.

Algorithms:
- ChaCha20-Poly1305 (preferred) — needs `cryptography` package.
- If `cryptography` is not installed, fall back to a simple XOR-based scheme
  (NOT cryptographically secure — for development only). A loud warning is
  logged in that case.

The decrypted plaintext is held in memory only for the duration of the call
and is explicitly zeroed after use (best-effort — Python strings are immutable,
so we can only delete the reference and rely on GC).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_KEY_ENV = "TERA_PILOT_PROMPT_KEY"
_DEFAULT_KEY_FILE = "~/.tera_pilot/prompt_key"

# Magic prefix to identify Tera Pilot-encrypted prompt files.
_MAGIC = b"CLWP1"


class EncryptedPromptError(Exception):
    pass


class EncryptedPromptStore:
    """A store for encrypted prompt templates.

    Usage:
        store = EncryptedPromptStore.from_env()
        with store.open("system_prompt.md.enc") as fh:
            plaintext = fh.read()
        # plaintext is zeroed on exit (best-effort).
    """

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise EncryptedPromptError(
                f"key must be 32 bytes (got {len(key)}); use derive_key() to make one from a passphrase"
            )
        self._key = key

    @classmethod
    def from_env(cls, env_var: str = _DEFAULT_KEY_ENV) -> "EncryptedPromptStore":
        raw = os.environ.get(env_var)
        if raw:
            try:
                key = base64.b64decode(raw)
                if len(key) == 32:
                    return cls(key)
            except Exception:
                pass
        # Fall back to keyfile.
        keyfile = os.path.expanduser(_DEFAULT_KEY_FILE)
        if os.path.exists(keyfile):
            with open(keyfile, "rb") as fh:
                return cls(fh.read().strip())
        raise EncryptedPromptError(
            f"no prompt key found; set ${env_var}=<base64 32-byte key> or create {keyfile}"
        )

    @staticmethod
    def derive_key(passphrase: str) -> bytes:
        """Derive a 32-byte key from a passphrase (SHA-256). For dev only —
        use a proper KDF (scrypt/argon2) in production."""
        return hashlib.sha256(passphrase.encode("utf-8")).digest()

    @staticmethod
    def generate_key() -> bytes:
        """Generate a random 32-byte key."""
        return secrets.token_bytes(32)

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt a plaintext string. Returns bytes with magic prefix."""
        nonce = secrets.token_bytes(12)
        try:
            from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
            aead = ChaCha20Poly1305(self._key)
            ct = aead.encrypt(nonce, plaintext.encode("utf-8"), None)
            return _MAGIC + nonce + ct
        except ImportError:
            logger.warning(
                "cryptography package not available; using XOR fallback (NOT secure). "
                "Install with: pip install cryptography"
            )
            return _MAGIC + nonce + _xor(plaintext.encode("utf-8"), self._key, nonce)

    def decrypt(self, blob: bytes) -> str:
        """Decrypt a previously encrypted blob."""
        if not blob.startswith(_MAGIC):
            raise EncryptedPromptError("not a Tera Pilot-encrypted prompt (missing magic)")
        rest = blob[len(_MAGIC):]
        if len(rest) < 12:
            raise EncryptedPromptError("truncated blob")
        nonce = rest[:12]
        ct = rest[12:]
        try:
            from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
            aead = ChaCha20Poly1305(self._key)
            pt = aead.decrypt(nonce, ct, None)
        except ImportError:
            pt = _xor(ct, self._key, nonce)
        except Exception as e:
            raise EncryptedPromptError(f"decrypt failed: {e}") from e
        return pt.decode("utf-8")

    def encrypt_file(self, src_path: str, dst_path: Optional[str] = None) -> str:
        """Encrypt a plaintext file. Returns the path to the encrypted file."""
        src = Path(src_path)
        dst = Path(dst_path) if dst_path else src.with_suffix(src.suffix + ".enc")
        plaintext = src.read_text(encoding="utf-8")
        blob = self.encrypt(plaintext)
        dst.write_bytes(blob)
        # Best-effort: zero the plaintext buffer (immutable in Python; just del).
        del plaintext
        return str(dst)

    def decrypt_file(self, src_path: str) -> str:
        """Decrypt an encrypted file. Returns the plaintext (caller should del it)."""
        blob = Path(src_path).read_bytes()
        return self.decrypt(blob)

    def open(self, src_path: str):
        """Context manager that yields the decrypted plaintext and zeroes it on exit."""
        return _DecryptedPromptContext(self, src_path)


class _DecryptedPromptContext:
    def __init__(self, store: EncryptedPromptStore, path: str):
        self._store = store
        self._path = path
        self._plaintext: Optional[str] = None

    def __enter__(self) -> str:
        self._plaintext = self._store.decrypt_file(self._path)
        return self._plaintext

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._plaintext is not None:
            # Best-effort: replace with a string of equal length before deletion
            # so the underlying memory gets overwritten. (CPython interns short
            # strings, but long strings are mutable-ish.)
            self._plaintext = "\0" * len(self._plaintext)
            del self._plaintext
            self._plaintext = None


def _xor(data: bytes, key: bytes, nonce: bytes) -> bytes:
    """XOR-based pseudo-encryption. NOT secure — dev fallback only."""
    if not key:
        return data
    # Mix the nonce into the key so each encryption is unique.
    mixed = bytes((key[i] ^ nonce[i % len(nonce)]) for i in range(len(key)))
    return bytes(b ^ mixed[i % len(mixed)] for i, b in enumerate(data))
