"""Explicit runtime configuration; no implicit provider choice."""

from dataclasses import dataclass
import math
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


@dataclass(frozen=True)
class PcmSegmentConfig:
    """16 kHz/s16le/mono segmentation; all durations are milliseconds.

    VAD operates on 32 ms windows. Minimum/wait durations round up; the hard
    maximum rounds down. This configuration is not wired into the running app.
    """

    silence_ms: int = 512
    min_speech_ms: int = 288
    max_segment_ms: int = 9984
    preroll_ms: int = 384
    threshold_start: float = 0.5
    threshold_continue: float = 0.3
    max_chunk_bytes: int = 320000

    def __post_init__(self):
        for name in ("silence_ms", "min_speech_ms", "max_segment_ms", "preroll_ms"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0 or (name != "preroll_ms" and value == 0)):
                raise ValueError(f"invalid PCM setting: {name}")
        for name in ("threshold_start", "threshold_continue"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError(f"invalid PCM setting: {name}")
        if self.threshold_continue > self.threshold_start:
            raise ValueError("PCM continuation threshold must not exceed start threshold")
        if (isinstance(self.max_chunk_bytes, bool) or not isinstance(self.max_chunk_bytes, int)
                or self.max_chunk_bytes <= 0):
            raise ValueError("PCM max_chunk_bytes must be a positive integer")
        if self.max_segment_ms // 32 < math.ceil(self.preroll_ms / 32) + math.ceil(self.min_speech_ms / 32):
            raise ValueError("PCM maximum must fit preroll and minimum speech")


def load_pcm_segment_config(env=None):
    """Read explicit PCM settings; never reads credentials or selects ASR/LLM."""
    env = os.environ if env is None else env
    defaults = PcmSegmentConfig()
    values = {}
    for name in defaults.__dataclass_fields__:
        raw = env.get(f"VOICE_PCM_{name.upper()}")
        cast = float if name.startswith("threshold_") else int
        values[name] = getattr(defaults, name) if raw in (None, "") else cast(raw)
    return PcmSegmentConfig(**values)
