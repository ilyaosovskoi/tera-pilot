"""
Custom Provider Loader — enables users to add their own AI providers
without modifying Tera Pilot source code.

Configuration: ~/.tera_pilot/providers.yaml
Plugin directory: ~/.tera_pilot/providers/
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

try:
    import yaml  # type: ignore[import]
except ImportError:  # pragma: no cover - exercised in minimal envs
    yaml = None  # type: ignore[assignment]

from .base import Provider, ProviderConfig
from .registry import ProviderRegistry

logger = logging.getLogger(__name__)


def _yaml_safe_load(text: str) -> Any:
    """Load YAML if pyyaml is installed, else return {}."""
    if yaml is None:
        logger.debug("[custom_providers] pyyaml not installed — skipping YAML config")
        return {}
    return yaml.safe_load(text)


def _yaml_dump(data: Any) -> str:
    """Dump YAML if pyyaml is installed, else fall back to repr()."""
    if yaml is None:
        return repr(data)
    return yaml.safe_dump(data, default_flow_style=False)


DEFAULT_PROVIDERS_DIR = Path.home() / ".tera_pilot" / "providers"
DEFAULT_CONFIG_PATH = Path.home() / ".tera_pilot" / "providers.yaml"


def get_providers_dir() -> Path:
    """Get the custom providers directory, creating it if needed."""
    DEFAULT_PROVIDERS_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_PROVIDERS_DIR


def get_config_path() -> Path:
    """Get the custom providers config path."""
    return DEFAULT_CONFIG_PATH


def load_custom_providers_config() -> List[Dict[str, Any]]:
    """Load custom provider configurations from YAML file.

    Returns list of dicts with keys:
      - provider_id: unique identifier
      - class_path: Python import path (e.g., "my_provider.MyProvider")
      - label: display name
      - default_model: model name
      - api_base: optional base URL
      - env_var: optional env var for API key
      - capabilities: list of capability strings
      - config: dict of additional ProviderConfig options
    """
    config_path = get_config_path()
    if not config_path.exists():
        return []

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = _yaml_safe_load(f.read()) or {}
        providers = data.get("providers", [])
        logger.info(f"[custom_providers] Loaded {len(providers)} custom provider configs from {config_path}")
        return providers
    except Exception as e:
        logger.warning(f"[custom_providers] Failed to load config from {config_path}: {e}")
        return []


def load_custom_provider_class(provider_config: Dict[str, Any]) -> Optional[Type[Provider]]:
    """Dynamically load a custom provider class from file or import path.

    Supports two modes:
    1. class_path: "my_provider.MyProvider" — import from Python path
    2. file_path: "~/.tera_pilot/providers/my_provider.py" — load from file
    """
    class_path = provider_config.get("class_path")
    file_path = provider_config.get("file_path")
    provider_id = provider_config.get("provider_id", "unknown")

    if class_path:
        try:
            module_path, class_name = class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            provider_cls = getattr(module, class_name)
            logger.info(f"[custom_providers] Loaded {provider_id} from import path: {class_path}")
            return provider_cls
        except Exception as e:
            logger.warning(f"[custom_providers] Failed to import {class_path} for {provider_id}: {e}")
            return None

    if file_path:
        try:
            expanded_path = Path(file_path).expanduser().resolve()
            if not expanded_path.exists():
                logger.warning(f"[custom_providers] Provider file not found: {expanded_path}")
                return None

            spec = importlib.util.spec_from_file_location(f"custom_provider_{provider_id}", expanded_path)
            if spec is None or spec.loader is None:
                logger.warning(f"[custom_providers] Could not load spec from {expanded_path}")
                return None

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find the Provider subclass in the module
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, Provider) and attr is not Provider:
                    logger.info(f"[custom_providers] Loaded {provider_id} from file: {expanded_path}")
                    return attr

            logger.warning(f"[custom_providers] No Provider subclass found in {expanded_path}")
            return None
        except Exception as e:
            logger.warning(f"[custom_providers] Failed to load {file_path} for {provider_id}: {e}")
            return None

    logger.warning(f"[custom_providers] No class_path or file_path specified for {provider_id}")
    return None


def create_dynamic_provider(provider_config: Dict[str, Any]) -> Optional[Type[Provider]]:
    """Create a dynamic provider class from config (for OpenAI-compatible APIs).

    This allows users to define OpenAI-compatible providers purely via YAML
    without writing Python code.
    """
    provider_id = provider_config.get("provider_id")
    if not provider_id:
        logger.warning("[custom_providers] No provider_id specified for dynamic provider")
        return None

    # Only create dynamic providers for OpenAI-compatible APIs
    if provider_config.get("type") != "openai_compatible":
        return None

    from .openai_compat import OpenAICompatProvider
    from .base import ProviderCapability

    label = provider_config.get("label", provider_id.title())
    default_model = provider_config.get("default_model", "")
    api_base = provider_config.get("api_base", "https://api.openai.com/v1")
    env_var = provider_config.get("env_var", f"{provider_id.upper()}_API_KEY")
    capabilities_list = provider_config.get("capabilities", ["chat", "streaming", "tool_calling", "system_prompt", "skills"])
    capabilities = frozenset(ProviderCapability(c) for c in capabilities_list)
    context_window = provider_config.get("context_window", 16_384)
    model_context_windows = provider_config.get("model_context_windows", {})

    # Create the dynamic class
    DynamicProvider = type(
        f"{provider_id.title().replace('-', '_').replace('_', '')}Provider",
        (OpenAICompatProvider,),
        {
            "provider_id": provider_id,
            "label": label,
            "default_model": default_model,
            "api_base": api_base,
            "env_var": env_var,
            "capabilities": capabilities,
            "context_window": context_window,
            "_MODEL_CONTEXT_WINDOWS": model_context_windows,
        }
    )

    logger.info(f"[custom_providers] Created dynamic OpenAI-compatible provider: {provider_id}")
    return DynamicProvider


def register_custom_providers(registry: ProviderRegistry) -> int:
    """Register all custom providers from config and plugin directory.

    Returns the number of successfully registered providers.
    """
    count = 0

    # 1. Load from YAML config
    configs = load_custom_providers_config()
    for config in configs:
        provider_id = config.get("provider_id")
        if not provider_id:
            continue

        # Skip if already registered (built-in takes precedence)
        if registry.has_provider(provider_id):
            logger.debug(f"[custom_providers] Skipping {provider_id} — already registered")
            continue

        provider_cls = None

        # Try dynamic creation first (OpenAI-compatible from YAML)
        provider_cls = create_dynamic_provider(config)

        # Then try loading from class_path or file_path
        if provider_cls is None:
            provider_cls = load_custom_provider_class(config)

        # Finally, check the providers directory for a matching .py file
        if provider_cls is None:
            providers_dir = get_providers_dir()
            for py_file in providers_dir.glob("*.py"):
                if py_file.stem == provider_id or py_file.stem.endswith(f"_{provider_id}"):
                    provider_cls = load_custom_provider_class({"provider_id": provider_id, "file_path": str(py_file)})
                    break

        if provider_cls:
            try:
                registry.register(provider_cls)
                count += 1
            except Exception as e:
                logger.warning(f"[custom_providers] Failed to register {provider_id}: {e}")

    # 2. Auto-discover .py files in providers directory (no config needed)
    providers_dir = get_providers_dir()
    for py_file in providers_dir.glob("*.py"):
        # Skip __pycache__ and __init__.py
        if py_file.name.startswith("__"):
            continue

        # Check if already registered
        module_name = py_file.stem
        provider_id = module_name.replace("-", "_")
        if registry.has_provider(provider_id):
            continue

        try:
            provider_cls = load_custom_provider_class({"provider_id": provider_id, "file_path": str(py_file)})
            if provider_cls:
                registry.register(provider_cls)
                count += 1
        except Exception as e:
            logger.warning(f"[custom_providers] Failed to auto-load {py_file}: {e}")

    if count > 0:
        logger.info(f"[custom_providers] Registered {count} custom provider(s)")

    return count


def create_example_config() -> str:
    """Generate an example providers.yaml for documentation."""
    return """# ~/.tera_pilot/providers.yaml
# Custom provider configuration for Tera Pilot
#
# Two ways to define providers:
# 1. OpenAI-compatible (dynamic, no code needed) — set type: openai_compatible
# 2. Custom Python class — specify class_path or file_path
#
# After editing, restart Tera Pilot or call registry.reload_custom_providers()

providers:
  # Example: OpenAI-compatible endpoint (e.g., local vLLM, LM Studio, Ollama with OpenAI API)
  - provider_id: "my_local_llm"
    type: "openai_compatible"
    label: "My Local LLM"
    default_model: "my-model"
    api_base: "http://localhost:8000/v1"
    env_var: "MY_LLM_API_KEY"
    capabilities:
      - "chat"
      - "streaming"
      - "tool_calling"
      - "system_prompt"
      - "skills"
    context_window: 32768
    model_context_windows:
      "my-model": 32768

  # Example: Custom Python provider class (import from installed package)
  - provider_id: "my_custom_provider"
    class_path: "my_package.providers.MyCustomProvider"
    label: "My Custom Provider"
    default_model: "custom-model"

  # Example: Custom Python provider from file
  - provider_id: "file_based_provider"
    file_path: "~/.tera_pilot/providers/my_provider.py"
    label: "File-Based Provider"
    default_model: "file-model"
"""