# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Single-task automation driver (upstream ``run_automation``).

Used by the ``artemis run`` CLI command in ``artemis.interfaces.cli.commands.run``.
"""

import os
from pathlib import Path
from typing import Annotated

import typer

from artemis import Agent, Builders
from artemis.config import initialize_llm_config, settings
from artemis.knowledge.integration import record_outcome, retrieve_for_run
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
from third_party.mobile_use.sdk.types.task import AgentProfile
from third_party.mobile_use.utils.video import check_ffmpeg_available

# CLI parameters of the ``run`` command.
GoalArgument = Annotated[str, typer.Argument(help="The main goal for the agent to achieve.")]
TestNameOption = Annotated[
    str | None,
    typer.Option(
        "--test-name",
        "-n",
        help="Name of the test run for trace recording.",
    ),
]
TracesPathOption = Annotated[
    str | None,
    typer.Option(
        "--traces-path",
        "-t",
        help="Directory where execution traces are persisted.",
    ),
]
OutputDescriptionOption = Annotated[
    str | None,
    typer.Option(
        "--output-description",
        "-o",
        help="Natural language or schema description of the expected output.",
    ),
]
VideoRecordingToolsOption = Annotated[
    bool | None,
    typer.Option(
        "--with-video-recording-tools/--without-video-recording-tools",
        help=(
            "Enable or disable dynamic video recording and screen analysis "
            "tools (auto-detected if omitted)."
        ),
    ),
]


def ensure_video_recording_available(with_video_recording_tools: bool | None) -> None:
    """Fail early when video recording tools are requested but ffmpeg is missing."""
    if with_video_recording_tools:
        check_ffmpeg_available()


def new_default_config_builder() -> AgentConfigBuilder:
    """Return a config builder set up with the default LLM profile and ADB server."""
    llm_config = initialize_llm_config()
    agent_profile = AgentProfile(name="default", llm_config=llm_config)
    config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
    if settings.ADB_HOST:
        config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)
    return config


async def run_automation(
    config: AgentConfigBuilder,
    goal: str,
    session_id: str | None = None,
    locked_app_package: str | None = None,
    test_name: str | None = None,
    traces_output_path_str: str | None = None,
    output_description: str | None = None,
    profile: str | None = None,
    app_path: str | None = None,
) -> None:
    """Initialize an agent from ``config``, run ``goal`` as a single task, then clean up."""
    agent: Agent | None = None
    # Workflow RAG: retrieve guidance *before* the agent starts, so the Planner is
    # the first and only consumer.  Advisory only -- when the knowledge base is
    # unconfigured, unreachable, or has nothing for this function, ``knowledge``
    # carries an empty block and the run behaves exactly as it did before.
    run_id = test_name or session_id or ""
    knowledge = retrieve_for_run(goal, app_package=locked_app_package, run_id=run_id)
    _log_knowledge(knowledge, profile)
    try:
        agent = Agent(config=config.build(), session_id=session_id)
        await agent.init(
            retry_count=int(os.getenv("ARTEMIS_HEALTH_RETRIES", 5)),
            retry_wait_seconds=int(os.getenv("ARTEMIS_HEALTH_DELAY", 2)),
        )

        task = agent.new_task(goal)
        if locked_app_package:
            task.with_locked_app_package(locked_app_package)
        if test_name:
            trace_path = traces_output_path_str or str(settings.TRACES_PATH)
            task.with_name(test_name).with_trace_recording(path=trace_path)
        if output_description:
            task.with_output_description(output_description)
        if profile:
            task.using_profile(profile)
        if app_path:
            task.with_app_path(Path(app_path))

        llm_result_path = os.getenv("RESULTS_OUTPUT_PATH", None)
        if llm_result_path:
            task.with_llm_output_saving(path=llm_result_path)

        result = await agent.run_task(request=task.build(), knowledge=knowledge)
        _record_run_outcome(run_id, result, agent)
    finally:
        if agent is not None:
            await agent.clean()


def _log_knowledge(knowledge, profile: str | None = None) -> None:
    """One line saying what RAG did for this run, and why if it did nothing.

    The flash warning is not cosmetic: the ``flash`` profile runs ``FlashRunner``,
    a single reactive loop with **no Planner**, so a retrieved workflow has
    nowhere to be consumed.  Without saying so, a tester would see "RAG: chèn
    kiến thức" and reasonably conclude the guidance was used.
    """
    if not knowledge.injected:
        print(f"  RAG: không chèn kiến thức — {knowledge.reason or 'không có workflow phù hợp'}")
        return
    # Nói rõ khối này đến từ đâu. Ở chế độ remote nó do console ghép và trả về; ở
    # chế độ tại chỗ ARTEMIS tự đọc database. Khi có sự cố thì đây là chi tiết đầu
    # tiên cần biết.
    where = "console ở xa" if knowledge.source == "console" else "database"
    app = f" · app {knowledge.app_id}" if knowledge.app_id else ""
    workflows = ", ".join(knowledge.trusted_ids) or "(chỉ có máy đoán)"
    print(
        f"  RAG: chèn kiến thức từ {where}: workflow {workflows}{app} "
        f"({knowledge.elapsed_ms:.0f}ms)"
    )
    if (profile or "").lower() == "flash":
        print(
            "  ⚠ Profile 'flash' chạy FlashRunner (KHÔNG có Planner) nên khối kiến thức này "
            "không được dùng. Chạy --profile pro để RAG có hiệu lực."
        )


def _record_run_outcome(run_id: str, result: object, agent: Agent) -> None:
    """Write the run's outcome back onto its retrieval log.

    ``Agent.run_task`` already attaches the machine-readable test summary
    (``task_status`` plus passed/failed/inconclusive) when the run declared check
    items, so this is a copy, not a re-derivation.  The task status is read as a
    fallback for runs that declared none.
    """
    outcome: dict = {}
    if isinstance(result, dict):
        summary = result.get("test_summary")
        if isinstance(summary, dict):
            summary = dict(summary)
            outcome["task_status"] = summary.pop("task_status", None)
            outcome["tests"] = summary
    status = _agent_task_status(agent)
    if status:
        outcome.setdefault("task_status", status)
    if not outcome:
        return
    updated = record_outcome(run_id, outcome)
    print(f"  RAG: ghi kết quả sử dụng vào {updated} bản ghi truy xuất")


def _agent_task_status(agent: Agent) -> str | None:
    for task in reversed(getattr(agent, "_tasks", None) or []):
        status = getattr(task, "status", None)
        if status is not None:
            return str(getattr(status, "value", status))
    return None
