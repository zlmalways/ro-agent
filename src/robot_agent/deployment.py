"""Explicit deployment assembly. No production secrets are fetched automatically."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import socket
import sys

import asyncpg

from robot_agent.adapters.asr import DoubaoASR, DoubaoStream
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


def build_app(configuration, *, environment=None):
    environment = os.environ if environment is None else environment
    deployment = configuration.get("deployment", {})
    enabled = deployment.get("execution_enabled") is True
    if enabled and not all(deployment.get(key) for key in ("host_id", "lock_path", "advisory_key")):
        raise ValueError("execution requires approved host, lock path and ownership advisory key")
    if enabled and (sys.platform != "linux" or deployment["host_id"] != socket.gethostname()
                    or not Path(deployment["lock_path"]).is_absolute()):
        raise ValueError("execution requires the approved Linux host and absolute shared lock path")
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
    users, robots = {}, {}
    for row in configuration.get("users", []):
        secret = environment.get(row["token_env"], "")
        if secret:
            users[secret] = {key: value for key, value in row.items() if key != "token_env"}
    for row in configuration.get("robots", []):
        secret = environment.get(row["token_env"], "")
        if secret:
            robots[secret] = {key: value for key, value in row.items() if key != "token_env"}
    if len(configuration.get("robots", [])) != 1:
        raise ValueError("first release requires exactly one explicitly provisioned robot")
    identity = ConfiguredIdentity(users=users, robots=robots)
    policy_config = configuration.get("policy", {})
    policy = ConfiguredPolicy(identity, version=policy_config.get("version", ""), actions=policy_config.get("actions", {}),
                              approval_ttl_seconds=policy_config.get("approval_ttl_seconds"))
    app = create_app(identity=identity)
    app.state.execution_enabled = enabled
    app.state.limits = limits

    @asynccontextmanager
    async def lifespan(app):
        pool = await asyncpg.create_pool(dsn, server_settings={"search_path": database.get("schema", "public")})
        runtime = None
        try:
            store = PostgresStore(pool, deployment_id=deployment["id"])
            await store.migrate()
            async with store.system_unit_of_work() as tx:
                for row in configuration["robots"]:
                    await tx.provision_binding(row)
            resources = Resources(store, freshness_seconds=limits["freshness_seconds"], max_entries=limits["max_resource_entries"])
            planner = OpenAICompatiblePlanner(load_provider_config(environment) if configuration.get("allow_external_calls") is True else None,
                                              max_response_bytes=limits["provider_response_bytes"])
            asr = DoubaoASR()
            if configuration.get("allow_external_calls") is True and environment.get("VOICE_ASR_PROVIDER") == "doubao":
                provider = DoubaoStream(api_key=environment.get("VOICE_ASR_DOUBAO_API_KEY", ""),
                                        resource_id=environment.get("VOICE_ASR_DOUBAO_RESOURCE_ID", ""),
                                        stream_url=environment.get("VOICE_ASR_DOUBAO_STREAM_URL", ""),
                                        timeout_seconds=limits["command_timeout"], max_response_bytes=limits["provider_response_bytes"])
                asr = DoubaoASR(provider)
            owner = RuntimeOwnership(dsn=dsn, deployment_id=deployment["id"], host_id=deployment["host_id"],
                                     lock_path=deployment["lock_path"], advisory_key=deployment["advisory_key"],
                                     schema=database.get("schema", "public")) if enabled else None
            service = TaskService(store=store, planner=planner, robot=None, resources=resources, policy=policy,
                                  catalog=ActionCatalog({"move_to": ("location",), "vla_skill": ("skill_id",)}),
                                  queue_policy=SubmissionOrderQueue(), ownership=owner,
                                  max_text_chars=limits["max_text_chars"], max_plan_steps=limits["max_plan_steps"])
            robot = Dlv0Robot(service=service, write_gate=owner, max_frame_bytes=limits["max_frame_bytes"])
            service.robot = robot
            app.state.service, app.state.resources, app.state.robot = service, resources, robot
            app.state.inputs = InputService(service, asr, max_audio_bytes=limits["max_audio_bytes"])
            if owner:
                runtime = Runtime(owner=owner, service=service, deployment_id=deployment["id"], scan_interval=limits["scan_interval"])
                await runtime.start()
            yield
        finally:
            if runtime:
                await runtime.close()
            await pool.close()
    app.router.lifespan_context = lifespan
    return app
