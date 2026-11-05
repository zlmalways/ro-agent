"""Connection-local PCM segmentation. Produces WAVs; never submits a task.

The caller owns a session per authenticated connection, feeds it serially, and
closes it on disconnect. Calls are synchronous: future async ingress must run
model creation/feed in a worker, not on the socket event loop.
"""

from collections import deque
from dataclasses import dataclass, field
import io
import math
from typing import Literal, Protocol
from uuid import uuid4
import wave

from robot_agent.config import PcmSegmentConfig, load_pcm_segment_config


_WINDOW_BYTES = 1024  # 512 signed 16-bit samples = 32 ms at 16 kHz
_WINDOW_MS = 32


class Vad(Protocol):
    def probability(self, pcm_window: bytes) -> float: ...
    def reset(self) -> None: ...


@dataclass(frozen=True)
class AudioSegment:
    segment_id: str
    wav_bytes: bytes
    duration_ms: int
    end_reason: Literal["silence", "max_duration"]


@dataclass(eq=False)
class PcmSession:
    """Opaque connection state created by PcmSegmentService.new_session()."""

    _owner: object
    _vad: Vad | None
    _pre: deque
    _pending: bytearray = field(default_factory=bytearray)
    _windows: list[bytes] = field(default_factory=list)
    _speech_windows: int = 0
    _silence_windows: int = 0
    _continuing: bool = False
    _closed: bool = False


def _silero_vad():
    # Lazy import keeps optional inference dependencies out of ordinary app startup.
    from robot_agent.adapters.silero_vad import SileroVad
    return SileroVad()


class PcmSegmentService:
    def __init__(self, config: PcmSegmentConfig | None = None, *, vad_factory=None):
        self.config = config if config is not None else load_pcm_segment_config()
        self._vad_factory = vad_factory if vad_factory is not None else _silero_vad
        self._owner = object()
        self._silence = math.ceil(self.config.silence_ms / _WINDOW_MS)
        self._minimum = math.ceil(self.config.min_speech_ms / _WINDOW_MS)
        self._maximum = self.config.max_segment_ms // _WINDOW_MS
        self._preroll = math.ceil(self.config.preroll_ms / _WINDOW_MS)

    def new_session(self) -> PcmSession:
        # A distinct stateful model per connection prevents cross-robot contamination.
        return PcmSession(self._owner, self._vad_factory(), deque(maxlen=self._preroll))

    def _check_session(self, session):
        if not isinstance(session, PcmSession) or session._owner is not self._owner:
            raise ValueError("PCM session does not belong to this service")
        if session._closed:
            raise ValueError("PCM session is closed")

    def close_session(self, session: PcmSession) -> None:
        """Discard unfinished audio, including partial samples; never emit a half sentence."""
        if not isinstance(session, PcmSession) or session._owner is not self._owner:
            raise ValueError("PCM session does not belong to this service")
        if session._closed:
            return
        session._closed = True
        session._pending.clear()
        session._pre.clear()
        session._windows.clear()
        session._speech_windows = session._silence_windows = 0
        session._continuing = False
        vad, session._vad = session._vad, None
        if vad is not None:
            vad.reset()

    def feed_pcm(self, session: PcmSession, pcm_bytes: bytes) -> list[AudioSegment]:
        """Accept raw s16le mono PCM at 16 kHz, returning zero or more complete WAVs.

        Network chunks may end mid-sample or contain multiple sentences. Buffering
        is bounded by the configured chunk/segment sizes and the preroll window.
        A max_duration result is truncated audio, NOT a complete command approval.
        """
        self._check_session(session)
        if not isinstance(pcm_bytes, bytes):
            raise TypeError("PCM input must be bytes")
        if len(pcm_bytes) > self.config.max_chunk_bytes:
            raise ValueError("PCM chunk exceeds max_chunk_bytes")
        session._pending.extend(pcm_bytes)
        results = []
        consumed = 0
        try:
            while len(session._pending) - consumed >= _WINDOW_BYTES:
                window = bytes(session._pending[consumed:consumed + _WINDOW_BYTES])
                consumed += _WINDOW_BYTES
                probability = session._vad.probability(window)
                if not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise ValueError("VAD returned an invalid probability")
                result = self._accept_window(session, window, probability)
                if result is not None:
                    results.append(result)
        except Exception:
            # Never turn a detector failure into 'silence' and submit truncated audio.
            try:
                self.close_session(session)
            except Exception:
                pass  # Preserve the original inference error; state is already cleared.
            raise
        del session._pending[:consumed]
        return results

    def _accept_window(self, session, window, probability):
        if not session._windows and not session._continuing:
            if probability <= self.config.threshold_start:
                session._pre.append(window)
                return None
            session._windows = list(session._pre)
            session._pre.clear()
            session._speech_windows = 1
            session._silence_windows = 0
        elif probability > self.config.threshold_continue:
            session._speech_windows += 1
            session._silence_windows = 0
        else:
            session._silence_windows += 1
        session._windows.append(window)
        # Prefer the actual natural endpoint when both conditions occur together.
        if session._silence_windows >= self._silence:
            return self._finish(session, "silence")
        if len(session._windows) >= self._maximum:
            return self._finish(session, "max_duration")
        return None

    def _finish(self, session, reason):
        windows, speech_windows = session._windows, session._speech_windows
        continuing = session._continuing
        accepted = speech_windows > 0 and (continuing or speech_windows >= self._minimum)
        session._windows = []
        session._speech_windows = 0
        session._continuing = reason == "max_duration" and (continuing or accepted)
        session._pre.clear()
        if not session._continuing:
            session._silence_windows = 0
            session._vad.reset()
        # Preroll and trailing silence must not make a click qualify as speech.
        # A forced split must not discard a short tail of the SAME utterance,
        # nor reset the detector's speech context before that tail arrives.
        if not accepted:
            return None
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"".join(windows))
        return AudioSegment(str(uuid4()), output.getvalue(), len(windows) * _WINDOW_MS, reason)
