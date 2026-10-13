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
    if selected not in {"gpt", "deepseek"}:
        return None
    if selected == "gpt":
        return ProviderConfig("gpt", env.get("GPT_BASE_URL", "").strip(), env.get("GPT_API_KEY", "").strip(),
                              env.get("GPT_MODEL", "").strip(), float(env.get("GPT_TIMEOUT_S", "30")))
    return ProviderConfig("deepseek", env.get("DEEPSEEK_BASE_URL", "").strip(),
                          env.get("DEEPSEEK_API_KEY", "").strip(), env.get("DEEPSEEK_MODEL", "").strip(),
                          float(env.get("DEEPSEEK_TIMEOUT_S", "30")))
