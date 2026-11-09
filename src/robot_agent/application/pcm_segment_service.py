"""Connection-local PCM segmentation. Produces WAVs; never submits a task.

The caller owns a session per authenticated connection, feeds it serially, and
closes it on disconnect. Calls are synchronous: future async ingress must run
model creation/feed in a worker, not on the socket event loop.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import io
import logging
import math
from pathlib import Path
import time
from typing import Literal, Protocol
from uuid import uuid4
import wave

from robot_agent.config import PcmSegmentConfig, load_pcm_segment_config


_WINDOW_BYTES = 1024  # 512 signed 16-bit samples = 32 ms at 16 kHz
_WINDOW_MS = 32
_RECORDING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
log = logging.getLogger(__name__)


class Vad(Protocol):
    def probability(self, pcm_window: bytes) -> float: ...
    def reset(self) -> None: ...


@dataclass(frozen=True)
class AudioSegment:
    segment_id: str
    wav_bytes: bytes
    duration_ms: int
    end_reason: Literal["silence", "max_duration"]


@dataclass(frozen=True)
class SpeechActivity:
    """Server-observed VAD activity; never inferred from merely receiving bytes."""

    segment_id: str
    started_at: float
    observed_at: float


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
    _segment_id: str | None = None
    _speech_started_at: float | None = None


def _silero_vad():
    # Lazy import keeps optional inference dependencies out of ordinary app startup.
    from robot_agent.adapters.silero_vad import SileroVad
    return SileroVad()


class PcmSegmentService:
    def __init__(self, config: PcmSegmentConfig | None = None, *, vad_factory=None, wav_save_dir=None):
        self.config = config if config is not None else load_pcm_segment_config()
        self._vad_factory = vad_factory if vad_factory is not None else _silero_vad
        self._owner = object()
        if wav_save_dir is not None and (not isinstance(wav_save_dir, (str, Path)) or not str(wav_save_dir).strip()):
            raise ValueError("wav_save_dir must be a non-empty path when configured")
        self._wav_save_dir = Path(wav_save_dir).expanduser() if wav_save_dir is not None else None
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
        session._segment_id = session._speech_started_at = None
        vad, session._vad = session._vad, None
        if vad is not None:
            vad.reset()

    def feed_pcm(self, session: PcmSession, pcm_bytes: bytes) -> list[AudioSegment]:
        """Keep the WAV-only API for existing callers."""
        return [event for event in self.feed_pcm_events(session, pcm_bytes)
                if isinstance(event, AudioSegment)]

    def feed_pcm_events(self, session: PcmSession, pcm_bytes: bytes) -> list[SpeechActivity | AudioSegment]:
        """Return ordered, immutable speech facts and completed WAVs.

        The worker owns VAD state only. Its caller applies speech activity to
        the wake service on the event loop, before awaiting ASR. Network chunk
        boundaries and silent windows never count as speech activity.
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
                results.extend(self._accept_window(session, window, probability, time.monotonic()))
        except Exception:
            # Never turn a detector failure into 'silence' and submit truncated audio.
            try:
                self.close_session(session)
            except Exception:
                pass  # Preserve the original inference error; state is already cleared.
            raise
        del session._pending[:consumed]
        return results

    def _accept_window(self, session, window, probability, observed_at):
        if not session._windows and not session._continuing:
            if probability <= self.config.threshold_start:
                session._pre.append(window)
                return []
            session._windows = list(session._pre)
            session._pre.clear()
            session._speech_windows = 1
            session._silence_windows = 0
            is_speech = True
        elif probability > self.config.threshold_continue:
            session._speech_windows += 1
            session._silence_windows = 0
            is_speech = True
        else:
            session._silence_windows += 1
            is_speech = False
        if session._segment_id is None:
            session._segment_id = str(uuid4())
        if is_speech and session._speech_started_at is None:
            session._speech_started_at = observed_at
        session._windows.append(window)
        events = []
        if is_speech and (session._continuing or session._speech_windows >= self._minimum):
            events.append(SpeechActivity(session._segment_id, session._speech_started_at, observed_at))
        # Keep both the natural endpoint and the 15-second audio cap unchanged.
        reason = None
        if session._silence_windows >= self._silence:
            reason = "silence"
        elif len(session._windows) >= self._maximum:
            reason = "max_duration"
        if reason is not None:
            segment = self._finish(session, reason)
            if segment is not None:
                events.append(segment)
        return events

    def _save_wav(self, segment_id: str, wav_bytes: bytes) -> None:
        """Save a completed segment without making persistence a processing gate."""
        if self._wav_save_dir is None:
            return
        now = datetime.now(timezone.utc).astimezone(_RECORDING_TIMEZONE)
        day_dir = self._wav_save_dir / now.strftime("%Y-%m-%d")
        path = day_dir / f"{now:%Y%m%d_%H%M%S_%f}_{segment_id}.wav"
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(wav_bytes)
        except OSError:
            log.exception("PCM WAV save failed segment=%s path=%s", segment_id, path)
            return
        log.info("PCM WAV saved segment=%s path=%s bytes=%d", segment_id, path, len(wav_bytes))

    def _finish(self, session, reason):
        windows, speech_windows = session._windows, session._speech_windows
        continuing = session._continuing
        segment_id = session._segment_id
        session._segment_id = session._speech_started_at = None
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
        wav_bytes = output.getvalue()
        self._save_wav(segment_id, wav_bytes)
        return AudioSegment(segment_id, wav_bytes, len(windows) * _WINDOW_MS, reason)
