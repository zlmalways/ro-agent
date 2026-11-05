"""CPU Silero adapter using packaged weights, with no runtime model download."""

import struct

from robot_agent.domain.models import DependencyUnavailable


class SileroVad:
    def __init__(self):
        try:
            import torch
            from silero_vad import load_silero_vad
            self._torch = torch
            self._model = load_silero_vad()
        except (ImportError, OSError, RuntimeError) as exc:
            raise DependencyUnavailable("PCM_VAD_UNAVAILABLE: install ro-agent[voice-pcm]") from exc

    def probability(self, pcm_window: bytes) -> float:
        if len(pcm_window) != 1024:
            raise ValueError("Silero expects a 512-sample PCM window")
        # Explicit little-endian decoding, without changing the original output PCM.
        tensor = self._torch.tensor(struct.unpack("<512h", pcm_window), dtype=self._torch.float32) / 32768
        with self._torch.inference_mode():
            return float(self._model(tensor, 16000).item())

    def reset(self) -> None:
        self._model.reset_states()
