from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from uuid import uuid4

from robot_agent.domain.models import ConflictError, DependencyUnavailable, PlanError, TaskInput


@dataclass(frozen=True)
class TranscribedAudio:
    input_id: str
    text: str
    provider_ref: str
    result_task_id: str | None = None
    asr_ms: float = 0.0
    reused: bool = False


class InputService:
    def __init__(self, service, asr, *, max_audio_bytes):
        self.service = service
        self.asr = asr
        self.max_audio_bytes = max_audio_bytes

    async def transcribe_audio(self, context, audio, content_type, request_key):
        """Persist and transcribe an input without creating or completing a task."""
        if not audio or len(audio) > self.max_audio_bytes:
            raise PlanError("AUDIO_INVALID")
        if not request_key:
            raise PlanError("IDEMPOTENCY_KEY_REQUIRED")
        digest = sha256(content_type.encode() + b"\0" + audio).hexdigest()
        async with self.service.store.unit_of_work(context) as tx:
            existing = await tx.get_input(context, request_key)
            if existing:
                if existing["request_digest"] != digest:
                    raise ConflictError("audio request key content conflict")
                if existing["state"] == "PROCESSING":
                    raise DependencyUnavailable("INPUT_PROCESSING")
                if existing["state"] == "FAILED":
                    raise DependencyUnavailable("INPUT_FAILED")
                return TranscribedAudio(
                    existing["input_id"], existing["normalized_text"] or "",
                    existing["provider_ref"] or "", existing["result_task_id"], reused=True,
                )
            if self.asr is None:
                raise DependencyUnavailable("ASR_UNAVAILABLE")
            input_id = str(uuid4())
            await tx.insert_input(context, request_key, input_id, digest)
        started = perf_counter()
        try:
            transcript = await self.asr.transcribe(audio)
        except (DependencyUnavailable, PlanError):
            async with self.service.store.unit_of_work(context) as tx:
                await tx.fail_input(context, request_key)
            raise
        asr_ms = (perf_counter() - started) * 1000
        text = getattr(transcript, "text", "").strip()
        provider_ref = getattr(transcript, "provider_ref", "")
        async with self.service.store.unit_of_work(context) as tx:
            await tx.finish_input(context, request_key, text, provider_ref)
        return TranscribedAudio(input_id, text, provider_ref, asr_ms=asr_ms)

    async def audio(self, context, audio, content_type, request_key, *, wait_for_plan=False, conversation_id=None):
        if conversation_id is not None:
            async with self.service.store.unit_of_work(context) as tx:
                await tx.get_conversation(conversation_id)
        transcript = await self.transcribe_audio(context, audio, content_type, request_key)
        if transcript.result_task_id is not None:
            async with self.service.store.unit_of_work(context) as tx:
                task = await tx.load_task(transcript.result_task_id)
                if conversation_id is not None and (task.conversation_id or task.task_id) != conversation_id:
                    raise ConflictError("audio request key content conflict")
                return await self.service.view_in_transaction(tx, task)
        if not transcript.text:
            raise PlanError("ASR_EMPTY")
        value = TaskInput(transcript.input_id, "audio", transcript.text, transcript.input_id)
        if conversation_id is None:
            view = await self.service.submit(context, value, transcript.input_id, wait_for_plan=wait_for_plan)
        else:
            view = await self.service.message(context, conversation_id, value, transcript.input_id)
        async with self.service.store.unit_of_work(context) as tx:
            await tx.finish_input(context, request_key, transcript.text, transcript.provider_ref, view.turn_id or view.task_id)
        return view

    async def submit_transcribed_audio(self, context, request_key, *, input_id, text, is_current=None):
        """Submit a trusted wake-approved command while retaining the ASR evidence."""
        if not isinstance(text, str) or not text.strip():
            raise PlanError("TEXT_REQUIRED")
        async with self.service.store.unit_of_work(context) as tx:
            stored = await tx.get_input(context, request_key)
            if stored is None or stored["input_id"] != input_id:
                raise PlanError("AUDIO_INPUT_MISMATCH")
            if stored["state"] not in {"TRANSCRIBED", "COMPLETE"}:
                raise PlanError("AUDIO_NOT_TRANSCRIBED")
            if stored["result_task_id"] is not None:
                return await self.service.view_in_transaction(tx, await tx.load_task(stored["result_task_id"]))
        if is_current is not None and not is_current():
            raise PlanError("VOICE_SESSION_REPLACED")
        view = await self.service.submit(
            context, TaskInput(input_id, "audio", text, input_id), input_id,
        )
        async with self.service.store.unit_of_work(context) as tx:
            await tx.finish_input(
                context, request_key, stored["normalized_text"], stored["provider_ref"], view.task_id,
            )
        return view

    def text(self, *, request_id, text):
        if not isinstance(text, str) or not text.strip():
            raise PlanError("TEXT_REQUIRED")
        return TaskInput(request_id, "web", text, "")
