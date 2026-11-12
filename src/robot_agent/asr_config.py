"""One ASR configuration boundary: ordinary JSON plus an explicit secrets JSON."""

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AsrConfig:
    provider: str
    api_key: str = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int
    model: str = ""
    resource_id: str = ""
    stream_url: str = ""


def read_credentials(configuration):
    value = configuration.get("credentials_file")
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ValueError("credentials_file must be an absolute JSON file path")
    try:
        credentials = json.loads(Path(value).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("credentials_file cannot be read as a JSON object") from None
    if not isinstance(credentials, dict):
        raise ValueError("credentials_file must contain a JSON object")
    return credentials


def load_asr_config(configuration, *, credentials=None):
    """Validate before opening a database, contacting a provider or starting a robot."""
    settings = configuration.get("voice_asr")
    if settings is None:
        return None  # Text-only deployments need no ASR or key file.
    if not isinstance(settings, dict):
        raise ValueError("voice_asr must be an object")
    provider = settings.get("provider")
    if provider not in ("disabled", "openai", "doubao"):
        raise ValueError("voice_asr.provider must be disabled, openai or doubao")
    if provider == "disabled" or configuration.get("allow_external_calls") is not True:
        return None

    def required_text(name):
        value = settings.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"voice_asr.{name} is required")
        return value.strip()

    timeout = settings.get("timeout_seconds")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("voice_asr.timeout_seconds must be a finite positive number")
    limits = configuration.get("limits", {})
    max_bytes = limits.get("provider_response_bytes") if isinstance(limits, dict) else None
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("limits.provider_response_bytes must be a positive integer")
    model, resource_id, stream_url = "", "", ""
    if provider == "openai":
        model = required_text("model")
        if settings.get("mode", "file") != "file":
            raise ValueError("voice_asr.mode must be file for openai")
    else:
        if settings.get("mode", "stream") != "stream":
            raise ValueError("voice_asr.mode must be stream for doubao")
        if settings.get("model", "bigmodel") != "bigmodel":
            raise ValueError("voice_asr.model must be bigmodel for doubao")
        model = "bigmodel"
        resource_id = required_text("resource_id")
        stream_url = required_text("stream_url")
        try:
            target = urlsplit(stream_url)
            valid = (target.scheme == "wss" and target.hostname and target.port != 0
                     and not (target.username or target.password or target.query or target.fragment))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("voice_asr.stream_url must be a wss URL without embedded credentials")
    credentials = read_credentials(configuration) if credentials is None else credentials
    if not isinstance(credentials, dict):
        raise ValueError("ASR credentials must be a JSON object")
    key_name = f"VOICE_ASR_{provider.upper()}_API_KEY"
    api_key = credentials.get(key_name)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(f"credentials_file is missing {key_name}")
    return AsrConfig(provider, api_key.strip(), timeout, max_bytes, model, resource_id, stream_url)
