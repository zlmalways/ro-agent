"""Explicit runtime configuration; no implicit provider choice."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float


def load_provider_config(env=None):
    env = os.environ if env is None else env
    selected = (env.get("DEEPLEAP_LLM_PROVIDER") or "").strip().lower()
    if selected not in {"gpt", "deepseek", "relay"}:
        return None
    prefix = selected.upper()
    return ProviderConfig(
        selected,
        env.get(f"{prefix}_BASE_URL", "").strip(),
        env.get(f"{prefix}_API_KEY", "").strip(),
        env.get(f"{prefix}_MODEL", "").strip(),
        float(env.get(f"{prefix}_TIMEOUT_S", "240" if selected == "relay" else "30")),
    )
