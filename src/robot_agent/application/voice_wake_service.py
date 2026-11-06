"""Match recorded ASR text and keep a short, robot-scoped wake window.

This service does not transcribe, create tasks or produce a spoken response.
The caller supplies trusted (customer_id, binding_id) identity, evaluates each
recorded input once. All state updates run serially on the platform event loop.
Wake state is process-local; disconnect alone does not alter the wake window.
"""

from dataclasses import dataclass, replace
import math
import time
from typing import Literal
import unicodedata


RobotKey = tuple[str, str]


def _separator(character: str) -> bool:
    return character.isspace() or unicodedata.category(character).startswith("P")


def _normalized(text: str) -> tuple[str, list[int]]:
    """Ignore spacing/punctuation while retaining original-text end offsets."""
    characters, ends = [], []
    for offset, original in enumerate(text):
        for character in unicodedata.normalize("NFKC", original).casefold():
            if not _separator(character):
                characters.append(character)
                ends.append(offset + 1)
    return "".join(characters), ends


def _command_tail(text: str) -> str:
    start = 0
    while start < len(text) and _separator(text[start]):
        start += 1
    return text[start:].strip()


@dataclass(frozen=True)
class VoiceWakeConfig:
    wake_word: str = "小千小千"
    aliases: tuple[str, ...] = ("小千", "小钱小钱", "小芊小芊")
    followup_seconds: float = 10.0

    def __post_init__(self):
        if not isinstance(self.wake_word, str) or not _normalized(self.wake_word)[0]:
            raise ValueError("voice wake_word must contain a non-punctuation character")
        if not isinstance(self.aliases, (tuple, list)):
            raise ValueError("voice wake aliases must be a list of strings")
        if any(not isinstance(alias, str) or not _normalized(alias)[0] for alias in self.aliases):
            raise ValueError("voice wake aliases must contain non-empty words")
        object.__setattr__(self, "aliases", tuple(self.aliases))
        if (isinstance(self.followup_seconds, bool)
                or not isinstance(self.followup_seconds, (int, float))
                or not math.isfinite(self.followup_seconds)
                or self.followup_seconds <= 0):
            raise ValueError("voice wake followup_seconds must be finite and positive")


@dataclass(frozen=True)
class FollowupAdmission:
    """Server-owned eligibility for one utterance, captured before waiting on ASR."""

    key: RobotKey
    segment_id: str
    accepted: bool
    reason: str
    source: str
    captured_at: float
    first_speech_at: float | None
    last_speech_at: float | None


@dataclass(frozen=True)
class WakeDecision:
    action: Literal["ignore", "wake_only", "command"]
    command: str
    matched_alias: str | None
    reason: str
    awake: bool
    remaining_ms: int
    evaluated_at: float
    deadline_at: float | None


class VoiceWakeService:
    def __init__(self, config: VoiceWakeConfig | None = None):
        self.config = config if config is not None else VoiceWakeConfig()
        # Deduplicate equivalent spellings; the canonical wake word wins ties.
        aliases = {}
        for alias in (self.config.wake_word, *self.config.aliases):
            aliases.setdefault(_normalized(alias)[0], alias)
        self._aliases = tuple(aliases.items())
        # Retain at most one window per trusted robot, even after it expires, so
        # a new utterance can distinguish expiry from never having been awake.
        self._deadlines: dict[RobotKey, float] = {}
        self._opened_at: dict[RobotKey, float] = {}

    @staticmethod
    def _check_key(key: RobotKey) -> None:
        if (not isinstance(key, tuple) or len(key) != 2
                or any(not isinstance(part, str) or not part.strip() for part in key)):
            raise ValueError("voice wake key must be trusted (customer_id, binding_id)")

    @staticmethod
    def _check_segment_id(segment_id: str) -> None:
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise ValueError("voice wake segment_id must be non-empty")

    @staticmethod
    def _check_time(value: float) -> None:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise ValueError("voice wake time must be finite monotonic seconds")

    @staticmethod
    def _check_admission(admission, key, *, segment_id=None, source=None):
        if not isinstance(admission, FollowupAdmission) or admission.key != key:
            raise ValueError("voice admission belongs to another robot")
        if segment_id is not None and admission.segment_id != segment_id:
            raise ValueError("voice admission belongs to another segment")
        if source is not None and admission.source != source:
            raise ValueError("voice admission has a different input source")

    def forget(self, key: RobotKey) -> None:
        """Explicitly clear one robot's state (not an automatic disconnect rule)."""
        self._check_key(key)
        self._deadlines.pop(key, None)
        self._opened_at.pop(key, None)

    def _capture(self, key, segment_id, source, at, first_speech_at=None, last_speech_at=None):
        opened_at = self._opened_at.get(key)
        deadline = self._deadlines.get(key)
        accepted = (opened_at is not None and deadline is not None
                    and opened_at <= at < deadline)
        reason = "awake_followup" if accepted else (
            "awake_expired" if deadline is not None and at >= deadline else "not_awake")
        return FollowupAdmission(key, segment_id, accepted, reason, source, at,
                                 first_speech_at, last_speech_at)

    def capture_input(self, key: RobotKey, segment_id: str) -> FollowupAdmission:
        """Snapshot eligibility for a whole WAV; uploading alone never renews it.

        This entrance has no real-time speech observations. Call it before ASR;
        neither a client wake flag nor the later transcript creates VAD evidence.
        """
        self._check_key(key)
        self._check_segment_id(segment_id)
        return self._capture(key, segment_id, "audio_upload", time.monotonic())

    def observe_speech(self, key: RobotKey, segment_id: str, *, observed_at: float,
                       started_at: float, admission: FollowupAdmission | None = None) -> FollowupAdmission:
        """Consume a real, qualifying VAD event on the platform event loop.

        The first qualifying event must fall inside the existing wake window.
        Once captured, this utterance's eligibility is stable across later events
        and ASR. Only accepted human-speech events renew the window; silence never
        calls this method. The candidate onset is evidence, not backdated consent.
        """
        self._check_key(key)
        self._check_segment_id(segment_id)
        self._check_time(observed_at)
        self._check_time(started_at)
        if started_at > observed_at:
            raise ValueError("speech onset cannot follow its observation")
        if admission is None:
            admission = self._capture(key, segment_id, "pcm_vad", observed_at,
                                      started_at, observed_at)
        else:
            self._check_admission(admission, key, segment_id=segment_id, source="pcm_vad")
            if (admission.first_speech_at != started_at or admission.last_speech_at is None
                    or observed_at < admission.last_speech_at):
                raise ValueError("speech observations must be ordered within one utterance")
            admission = replace(admission, last_speech_at=observed_at)
        if admission.accepted:
            # A late older event must not shorten a more recent explicit wake.
            renewed = observed_at + self.config.followup_seconds
            self._deadlines[key] = max(self._deadlines.get(key, renewed), renewed)
        return admission

    def _decision(self, key, now, action, command, alias, reason):
        deadline = self._deadlines.get(key)
        awake = deadline is not None and now < deadline
        remaining_ms = math.ceil((deadline - now) * 1000) if awake else 0
        return WakeDecision(action, command, alias, reason, awake, remaining_ms, now, deadline)

    def evaluate(self, key: RobotKey, text: str, *,
                 admission: FollowupAdmission | None = None) -> WakeDecision:
        """Match ASR text using the eligibility already captured for this input.

        Ordinary ASR completion never renews the window or revokes an accepted
        utterance. Recognizing an explicit wake word still opens a new window.
        Matching keeps the text after the LAST alias, preferring the longest
        alias when several matches end at the same original-text position.
        """
        self._check_key(key)
        if not isinstance(text, str):
            raise TypeError("voice wake input must be ASR text")
        if admission is None:
            admission = self.capture_input(key, "text-evaluation")
        else:
            self._check_admission(admission, key)
        now = time.monotonic()
        normalized, ends = _normalized(text)
        match = None
        for spelling, alias in self._aliases:
            start = normalized.rfind(spelling)
            if start >= 0:
                candidate = (start + len(spelling), len(spelling), alias)
                if match is None or candidate[:2] > match[:2]:
                    match = candidate
        if match is not None:
            end, _, alias = match
            command = _command_tail(text[ends[end - 1]:])
            self._opened_at[key] = now
            self._deadlines[key] = now + self.config.followup_seconds
            return self._decision(
                key, now, "command" if command else "wake_only", command, alias,
                "wake_word_with_command" if command else "wake_word_only",
            )
        if not normalized:
            return self._decision(key, now, "ignore", "", None, "empty_text")
        if admission.accepted:
            return self._decision(key, now, "command", text.strip(), None, "awake_followup")
        return self._decision(key, now, "ignore", "", None, admission.reason)
