from hashlib import sha256
from uuid import uuid4

from robot_agent.domain.models import ConflictError, DependencyUnavailable, PlanError, TaskInput


class InputService:
    def __init__(self, service, asr, *, max_audio_bytes):
        self.service = service
        self.asr = asr
        self.max_audio_bytes = max_audio_bytes

    async def audio(self, context, audio, content_type, request_key):
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
                if existing["state"] == "COMPLETE":
                    from .task_service import task_view
                    return task_view(await tx.load_task(existing["result_task_id"]))
                if existing["state"] == "PROCESSING":
                    raise DependencyUnavailable("INPUT_PROCESSING")
                if existing["state"] == "FAILED":
                    raise DependencyUnavailable("INPUT_FAILED")
                input_id, text, provider_ref = existing["input_id"], existing["normalized_text"], existing["provider_ref"]
            else:
                if self.asr is None:
                    raise DependencyUnavailable("ASR_UNAVAILABLE")
                input_id, text, provider_ref = str(uuid4()), None, ""
                await tx.insert_input(context, request_key, input_id, digest)
        if text is None:
            try:
                transcript = await self.asr.transcribe(audio)
            except (DependencyUnavailable, PlanError):
                async with self.service.store.unit_of_work(context) as tx:
                    await tx.fail_input(context, request_key)
                raise
            text, provider_ref = getattr(transcript, "text", "").strip(), getattr(transcript, "provider_ref", "")
            async with self.service.store.unit_of_work(context) as tx:
                await tx.finish_input(context, request_key, text, provider_ref)
        if not text:
            raise PlanError("ASR_EMPTY")
        view = await self.service.submit(context, TaskInput(input_id, "audio", text, input_id), input_id)
        async with self.service.store.unit_of_work(context) as tx:
            await tx.finish_input(context, request_key, text, provider_ref, view.task_id)
        return view

    def text(self, *, request_id, text):
        if not isinstance(text, str) or not text.strip():
            raise PlanError("TEXT_REQUIRED")
        return TaskInput(request_id, "web", text, "")
