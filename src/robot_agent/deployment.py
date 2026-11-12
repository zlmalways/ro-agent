"""Explicit deployment assembly. No production secrets are fetched automatically."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import socket

import asyncpg

from robot_agent.adapters.asr import DoubaoASR, DoubaoStream, OpenAIASR
from robot_agent.asr_config import load_asr_config
from robot_agent.adapters.dlv0 import Dlv0Robot
from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.adapters.llm import OpenAICompatiblePlanner
from robot_agent.adapters.policy import ConfiguredPolicy
from robot_agent.adapters.postgres import PostgresStore
from robot_agent.app import create_app
from robot_agent.application.input_service import InputService
from robot_agent.application.resources import Resources
from robot_agent.application.task_service import TaskService
from robot_agent.config import load_provider_config
from robot_agent.domain.models import ActionCatalog
from robot_agent.runtime import Runtime, RuntimeOwnership, SubmissionOrderQueue


_ASR_NOT_LOADED = object()


def _pcm_settings(configuration):
    settings = configuration.get("voice_pcm", {})
    if not isinstance(settings, dict) or any(
        not isinstance(settings.get(key, False), bool) for key in ("enabled", "submit_tasks", "evaluate_wake")
    ):
        raise ValueError("voice_pcm.enabled, submit_tasks and evaluate_wake must be booleans")
    if (settings.get("submit_tasks") or settings.get("evaluate_wake")) and not settings.get("enabled"):
        raise ValueError("PCM processing requires PCM capture to be enabled")
    wav_save_dir = settings.get("wav_save_dir")
    if wav_save_dir is not None and (not isinstance(wav_save_dir, str) or not wav_save_dir.strip()):
        raise ValueError("voice_pcm.wav_save_dir must be a non-empty string when configured")
    return settings


def build_pcm_segments(configuration, environment):
    """Capture remains opt-in; task submission has a separate explicit switch."""
    settings = _pcm_settings(configuration)
    if not settings.get("enabled", False):
        return None
    from robot_agent.application.pcm_segment_service import PcmSegmentService
    from robot_agent.config import load_pcm_segment_config
    return PcmSegmentService(load_pcm_segment_config(environment), wav_save_dir=settings.get("wav_save_dir"))


def build_pcm_voice(configuration, inputs):
    """With both switches enabled, only wake-approved PCM creates tasks."""
    settings = _pcm_settings(configuration)
    if not settings.get("submit_tasks") and not settings.get("evaluate_wake"):
        return None
    from robot_agent.adapters.voice_output import VoiceOutput
    from robot_agent.application.voice_service import VoiceService
    evaluation = None
    if settings.get("evaluate_wake"):
        from robot_agent.application.voice_wake_evaluation import VoiceWakeEvaluation
        from robot_agent.application.voice_wake_service import VoiceWakeConfig, VoiceWakeService
        wake_settings = configuration.get("voice_wake", {})
        if not isinstance(wake_settings, dict):
            raise ValueError("voice_wake must be an object")
        evaluation = VoiceWakeEvaluation(inputs, VoiceWakeService(VoiceWakeConfig(**wake_settings)))
    submit_tasks = settings.get("submit_tasks", False)
    return VoiceService(inputs=inputs, output=None if evaluation else VoiceOutput(None),
                        allow_woken_audio=False, allow_unwoken_pcm=submit_tasks and evaluation is None,
                        wake_evaluation=evaluation, submit_woken_pcm=submit_tasks and evaluation is not None,
                        max_audio_bytes=configuration["limits"]["max_audio_bytes"])


def build_asr(settings):
    """Assemble an adapter from already validated settings, without reading env/files."""
    if settings is None:
        return DoubaoASR()  # Unconfigured ASR fails on use; text tasks can still run.
    if settings.provider == "openai":
        return OpenAIASR(
            api_key=settings.api_key, model=settings.model,
            timeout_seconds=settings.timeout_seconds, max_response_bytes=settings.max_response_bytes,
        )
    if settings.provider != "doubao":
        raise ValueError("unsupported ASR provider")
    return DoubaoASR(DoubaoStream(
        api_key=settings.api_key, resource_id=settings.resource_id, stream_url=settings.stream_url,
        timeout_seconds=settings.timeout_seconds, max_response_bytes=settings.max_response_bytes,
    ))


def build_app(configuration, *, environment=None, asr_config=_ASR_NOT_LOADED):
    environment = os.environ if environment is None else environment
    if configuration.get("allow_external_calls") is not True:
        asr_config = None
    elif asr_config is _ASR_NOT_LOADED:
        asr_config = load_asr_config(configuration)
    deployment = configuration.get("deployment", {})
    enabled = deployment.get("execution_enabled") is True
    if enabled and not all(deployment.get(key) for key in ("host_id", "lock_path", "advisory_key")):
        raise ValueError("execution requires approved host, lock path and ownership advisory key")
    if enabled and (deployment["host_id"] != socket.gethostname() or not Path(deployment["lock_path"]).is_absolute()):
        raise ValueError("execution requires the approved host and absolute shared lock path")
    if "database" not in configuration or "limits" not in configuration:
        raise ValueError("explicit database and operation limits are required")
    database = configuration["database"]
    limits = configuration["limits"]
    required_limits = ("freshness_seconds", "max_resource_entries", "max_audio_bytes", "max_frame_bytes", "max_text_chars",
                       "max_plan_steps", "provider_response_bytes", "scan_interval", "command_timeout")
    if any(key not in limits or not isinstance(limits[key], (int, float)) or limits[key] <= 0 for key in required_limits):
        raise ValueError("explicit positive operation limits are required")
    dsn = environment.get(database["dsn_env"], "")
    if not dsn:
        raise ValueError("database secret reference is not configured")
    use_reported_waypoints = configuration.get("resources", {}).get("use_reported_waypoints", False)
    if not isinstance(use_reported_waypoints, bool):
        raise ValueError("resources.use_reported_waypoints must be a boolean")
    users, robots = {}, {}
    customer_owners = {}
    for row in configuration.get("users", []):
        owner = customer_owners.setdefault(row["customer_id"], row["actor_id"])
        if owner != row["actor_id"]:
            raise ValueError("each customer space must belong to one user account")
        secret = environment.get(row["token_env"], "")
        if secret:
            users[secret] = {key: value for key, value in row.items() if key != "token_env"}
    bindings = set()
    missing_credentials = set()
    for row in configuration.get("robots", []):
        key = (row["customer_id"], row["binding_id"])
        if key in bindings:
            raise ValueError("duplicate robot binding")
        bindings.add(key)
        secret = environment.get(row["token_env"], "")
        if secret:
            if secret in robots:
                raise ValueError("duplicate robot credential")
            robots[secret] = {key: value for key, value in row.items() if key != "token_env"}
        else:
            missing_credentials.add(key)
    identity = ConfiguredIdentity(users=users, robots=robots)
    policy_config = configuration.get("policy", {})
    policy = ConfiguredPolicy(identity, version=policy_config.get("version", ""), actions=policy_config.get("actions", {}),
                              approval_ttl_seconds=policy_config.get("approval_ttl_seconds"))
    app = create_app(identity=identity)
    app.state.execution_enabled = enabled
    app.state.limits = limits
    app.state.pcm_segments = build_pcm_segments(configuration, environment)

    @asynccontextmanager
    async def lifespan(app):
        pool = await asyncpg.create_pool(dsn, server_settings={"search_path": database.get("schema", "public")})
        runtime = None
        voice = None
        try:
            store = PostgresStore(pool, deployment_id=deployment["id"])
            await store.migrate()
            async with store.system_unit_of_work() as tx:
                for row in configuration.get("robots", []):
                    await tx.provision_binding(row)
            resources = Resources(store, freshness_seconds=limits["freshness_seconds"],
                                  max_entries=limits["max_resource_entries"], use_reported_waypoints=use_reported_waypoints,
                                  missing_credentials=missing_credentials)
            planner = OpenAICompatiblePlanner(
                load_provider_config(environment) if configuration.get("allow_external_calls") is True else None,
                max_response_bytes=limits["provider_response_bytes"],
                history_loader=store.conversation_messages,
                audit_store=store)
            asr = build_asr(asr_config)
            owner = RuntimeOwnership(dsn=dsn, deployment_id=deployment["id"], host_id=deployment["host_id"],
                                     lock_path=deployment["lock_path"], advisory_key=deployment["advisory_key"],
                                     schema=database.get("schema", "public")) if enabled else None
            service = TaskService(store=store, planner=planner, robot=None, resources=resources, policy=policy,
                                  catalog=ActionCatalog({"move_to": ("location",), "vla_skill": ("skill_id",)}),
                                  queue_policy=SubmissionOrderQueue(), ownership=owner,
                                  max_text_chars=limits["max_text_chars"], max_plan_steps=limits["max_plan_steps"],
                                  command_timeout_seconds=limits["command_timeout"])
            robot = Dlv0Robot(service=service, write_gate=owner, max_frame_bytes=limits["max_frame_bytes"])
            service.robot = robot
            resources.connected_robots = robot.connected_robots
            app.state.service, app.state.resources, app.state.robot = service, resources, robot
            app.state.inputs = InputService(service, asr, max_audio_bytes=limits["max_audio_bytes"])
            voice = build_pcm_voice(configuration, app.state.inputs)
            if voice is not None:
                app.state.voice = voice
            runtime = Runtime(owner=owner, service=service, deployment_id=deployment["id"], scan_interval=limits["scan_interval"])
            await runtime.start()
            app.state.runtime = runtime
            yield
        finally:
            try:
                if runtime:
                    await runtime.close()
            finally:
                try:
                    if voice is not None:
                        await voice.close()
                finally:
                    await pool.close()
    app.router.lifespan_context = lifespan
    return app
