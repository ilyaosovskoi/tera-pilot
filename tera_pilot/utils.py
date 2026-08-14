"""
Utility functions for Tera Pilot
"""

import os
import sys
import logging
import json
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from datetime import datetime
from functools import wraps


# ── Retry Decorator ─────────────────────────────────────────────────────

def retry_on_error(max_retries: int = 3, delay: float = 0.5, backoff: float = 2.0,
                   exceptions: tuple = (Exception,)):
    """Retry decorator with exponential backoff."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries:
                        raise
                    logging.getLogger(__name__).warning(
                        f"[retry] {func.__name__} failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {current_delay:.1f}s..."
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff
            return None  # unreachable
        return wrapper
    return decorator


# ── Cached System Info ──────────────────────────────────────────────────

_system_info_cache: Optional[Dict[str, Any]] = None
_system_info_lock = threading.Lock()


def get_system_info() -> Dict[str, Any]:
    """Get macOS system information (cached with thread-safe lazy init)."""
    global _system_info_cache

    if _system_info_cache is not None:
        return _system_info_cache.copy()

    with _system_info_lock:
        # Double-check after acquiring lock
        if _system_info_cache is not None:
            return _system_info_cache.copy()

        import platform
        import subprocess

        info = {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "is_apple_silicon": platform.machine() == "arm64",
        }

        # Try to get memory info
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, check=True
            )
            total_ram = int(result.stdout.strip())
            info["total_ram_gb"] = round(total_ram / (1024**3), 1)
        except Exception:
            info["total_ram_gb"] = None

        # Try to get CPU cores
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.ncpu"],
                capture_output=True, text=True, check=True
            )
            info["cpu_cores"] = int(result.stdout.strip())
        except Exception:
            info["cpu_cores"] = None

        _system_info_cache = info
        return info.copy()


def invalidate_system_info_cache():
    """Invalidate the system info cache (useful for testing)."""
    global _system_info_cache
    with _system_info_lock:
        _system_info_cache = None


# ── Logging ─────────────────────────────────────────────────────────────

def setup_logging(level: int = logging.INFO):
    """Configure application logging."""
    log_dir = Path.home() / ".tera_pilot" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"tera_pilot_{datetime.now().strftime('%Y%m%d')}.log"

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ── Paths ───────────────────────────────────────────────────────────────

def get_resource_path(relative_path: str) -> str:
    """Get absolute path to a resource file."""
    if getattr(sys, "frozen", False):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).parent
    return str(base_path / relative_path)


def get_tera_pilot_dir() -> Path:
    """Get Tera Pilot configuration directory."""
    tera_pilot_dir = Path.home() / ".tera_pilot"
    tera_pilot_dir.mkdir(parents=True, exist_ok=True)
    return tera_pilot_dir


# ── Config (Atomic Writes) ────────────────────────────────────────────

_config_lock = threading.Lock()


def load_config() -> Dict[str, Any]:
    """Load Tera Pilot configuration."""
    config_path = get_tera_pilot_dir() / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logging.getLogger(__name__).warning(f"Failed to load config: {e}. Using defaults.")
    return get_default_config()


def save_config(config: Dict[str, Any]):
    """Save Tera Pilot configuration atomically (no partial writes on crash)."""
    config_path = get_tera_pilot_dir() / "config.json"
    tmp_path = config_path.with_suffix(".tmp")

    with _config_lock:
        # Write to temp file first
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # Ensure data hits disk

        # Atomic replace
        os.replace(tmp_path, config_path)


def get_default_config() -> Dict[str, Any]:
    """Get default configuration."""
    return {
        "version": "1.0.0",
        "ui": {
            "theme": "space-gray",
            "font_size": 14,
            "line_height": 1.7,
            "show_minimap": True,
            "word_wrap": True,
        },

        "inference": {
            "default_max_tokens": 4096,
            "default_temperature": 0.2,
            "default_top_p": 0.9,
            "default_top_k": 50,
            "stream_tokens": True,
            "context_window": 131072,
        },

        "agent": {
            "max_iterations": 8,
            "enable_planning": True,
            "auto_accept_safe": False,
            "tool_timeout": 15,
        },
        "providers": {
            "openai": {"api_key": None, "base_url": None},
            "anthropic": {"api_key": None, "base_url": None},
            "openrouter": {"api_key": None, "base_url": "https://openrouter.ai/api/v1"},
        },
        "editor": {
            "tab_size": 4,
            "use_spaces": True,
            "auto_save": True,
            "auto_format": True,
            "line_length": 100,
        },
    }


# ── Formatting ──────────────────────────────────────────────────────────

def format_bytes(size: int) -> str:
    """Format bytes to human readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


# ── Model Memory Estimation ─────────────────────────────────────────────

def estimate_model_memory(
    params_b: float,
    quantization: str = "bf16",
    context_length: int = 4096,
    num_layers: int = 40,
    head_dim: int = 128,
    num_kv_heads: int = 8,
) -> Dict[str, float]:
    """Estimate memory usage for a model in GB."""
    bytes_per_param = {
        "f32": 4, "fp32": 4,
        "f16": 2, "fp16": 2,
        "bf16": 2,
        "q8": 1, "q8_0": 1, "int8": 1,
        "q4": 0.5, "q4_0": 0.5, "q4_k": 0.5,
        "q4_k_m": 0.55, "q4_k_s": 0.45,
        "q6_k": 0.75,
        "q5": 0.625, "q5_0": 0.625, "q5_k": 0.625,
    }.get(quantization, 2)

    weights_gb = params_b * 1e9 * bytes_per_param / 1e9

    kv_bytes = bytes_per_param
    kv_cache_gb = 2 * num_layers * num_kv_heads * head_dim * context_length * kv_bytes / 1e9

    activations_gb = num_layers * head_dim * 4 * 2 / 1e9
    overhead_gb = 0.5
    total_gb = weights_gb + kv_cache_gb + activations_gb + overhead_gb

    return {
        "weights_gb": round(weights_gb, 2),
        "kv_cache_gb": round(kv_cache_gb, 2),
        "activations_gb": round(activations_gb, 2),
        "overhead_gb": overhead_gb,
        "total_gb": round(total_gb, 2),
    }