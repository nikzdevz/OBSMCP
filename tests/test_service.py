from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from server.config import AppConfig, ObsidianConfig, TaskOutputOverrideConfig
from server.service import ObsmcpService


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        local_temp_root = Path(__file__).resolve().parent.parent / ".tmp-tests"
        local_temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = local_temp_root / f"obsmcp-test-{uuid.uuid4().hex[:8]}"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.config = AppConfig(
            root_dir=self.temp_dir,
            app_name="obsmcp",
            description="test",
            host="127.0.0.1",
            port=9300,
            bind_local_only=True,
            database_path=self.temp_dir / "data" / "db" / "obsmcp.sqlite3",
            json_export_dir=self.temp_dir / "data" / "json",
            backup_dir=self.temp_dir / "data" / "backups",
            log_dir=self.temp_dir / "logs",
            context_dir=self.temp_dir / ".context",
            obsidian_vault_dir=self.temp_dir / "obsidian" / "vault",
            pid_file=self.temp_dir / "data" / "obsmcp.pid",
            max_recent_work_items=12,
            max_decisions=20,
            max_blockers=20,
            api_token=None,
            obsidian=ObsidianConfig(
                project_brief_note="Projects/Project Brief.md",
                current_task_note="Projects/Current Task.md",
                status_snapshot_note="Projects/Status Snapshot.md",
                latest_handoff_note="Handoffs/Latest Handoff.md",
                decision_index_note="Decisions/Decision Log.md",
                daily_notes_dir="Daily",
                session_note="Sessions/Latest Session Summary.md",
                code_atlas_note="Research/Code Atlas.md",
            ),
        )
        self.config.ensure_directories()
        self.service = ObsmcpService(self.config)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_continuity_flow_syncs_context_and_obsidian(self) -> None:
        project_paths = self.service.get_project_workspace_paths(project_path=str(self.temp_dir))
        task = self.service.create_task(
            title="Implement continuity",
            description="Create shared state and sync outputs.",
            relevant_files=["server/main.py", "cli/main.py"],
            actor="test",
        )
        self.service.set_current_task(task_id=task["id"], actor="test")
        self.service.log_work(
            message="Implemented shared continuity flow.",
            task_id=task["id"],
            files=["server/service.py"],
            actor="test",
        )
        self.service.log_decision(
            title="Use SQLite",
            decision="SQLite is the local source of truth.",
            rationale="Minimal ops burden.",
            impact="Single-file backup and recovery.",
            task_id=task["id"],
            actor="test",
        )
        self.service.create_handoff(
            summary="Core flow is implemented and synced.",
            next_steps="Add more client integrations.",
            task_id=task["id"],
            from_actor="test",
            to_actor="next-model",
        )
        self.service.create_daily_note_entry("Validated sync path.", actor="test")

        context_dir = Path(project_paths["context_path"])
        vault_dir = Path(project_paths["vault_path"])
        current_task = json.loads((context_dir / "CURRENT_TASK.json").read_text(encoding="utf-8"))
        self.assertEqual(current_task["id"], task["id"])
        self.assertTrue((context_dir / "HANDOFF.md").exists())
        self.assertTrue((context_dir / "RESUME_PACKET.md").exists())
        self.assertTrue((vault_dir / "Projects" / "Project Brief.md").exists())
        self.assertTrue((vault_dir / "Decisions" / "ADR-0001.md").exists())

    def test_generate_compact_context(self) -> None:
        task = self.service.create_task(
            title="Compact context",
            description="Keep token usage low.",
            actor="test",
        )
        self.service.set_current_task(task_id=task["id"], actor="test")
        content = self.service.generate_compact_context()
        self.assertIn("Compact Context", content)
        self.assertIn(task["id"], content)

    def test_startup_does_not_bootstrap_default_project_by_default(self) -> None:
        default_repo = self.temp_dir / "default-repo"
        default_repo.mkdir()
        self.config.default_project_path = default_repo

        service = ObsmcpService(self.config)

        self.assertEqual(service._stores, {})
        self.assertIsNone(service.store)
        self.assertEqual(list((self.temp_dir / "projects").iterdir()), [])

    def test_health_check_without_project_does_not_create_default_project(self) -> None:
        default_repo = self.temp_dir / "default-repo"
        default_repo.mkdir()
        self.config.default_project_path = default_repo

        service = ObsmcpService(self.config)
        status = service.health_check()

        self.assertIsNone(status["project_path"])
        self.assertFalse(status["db_exists"])
        self.assertFalse(status["bootstrap_default_project_on_startup"])
        self.assertEqual(list((self.temp_dir / "projects").iterdir()), [])

    def test_startup_can_bootstrap_default_project_when_enabled(self) -> None:
        default_repo = self.temp_dir / "default-repo"
        default_repo.mkdir()
        self.config.default_project_path = default_repo
        self.config.bootstrap_default_project_on_startup = True

        service = ObsmcpService(self.config)

        self.assertIsNotNone(service.store)
        self.assertEqual(len(service._stores), 1)
        self.assertTrue(any((self.temp_dir / "projects").iterdir()))

    def test_generate_context_profile_caches_and_refreshes(self) -> None:
        task = self.service.create_task(
            title="Tiered context",
            description="Verify cached context profiles refresh on writes.",
            relevant_files=["server/service.py"],
            actor="test",
        )
        self.service.set_current_task(task_id=task["id"], actor="test")

        first = self.service.generate_context_profile(profile="balanced", task_id=task["id"], max_tokens=1800)
        self.assertFalse(first["cached"])
        self.assertIn(task["id"], first["markdown"])

        second = self.service.generate_context_profile(profile="balanced", task_id=task["id"], max_tokens=1800)
        self.assertTrue(second["cached"])

        self.service.log_work(
            message="Added a fresh work log to invalidate cached context.",
            task_id=task["id"],
            files=["server/store.py"],
            actor="test",
        )

        refreshed = self.service.generate_context_profile(profile="balanced", task_id=task["id"], max_tokens=1800)
        self.assertFalse(refreshed["cached"])
        self.assertIn("Added a fresh work log", refreshed["markdown"])

    def test_generate_delta_context_since_handoff(self) -> None:
        repo = self.temp_dir / "delta-repo"
        repo.mkdir()
        task = self.service.create_task(
            title="Delta context",
            description="Track only recent changes after a handoff.",
            relevant_files=[str(repo / "app.py")],
            actor="test",
            project_path=str(repo),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(repo))
        handoff = self.service.create_handoff(
            summary="Baseline handoff before new changes.",
            next_steps="Continue after new writes.",
            task_id=task["id"],
            from_actor="test",
            to_actor="next-agent",
            project_path=str(repo),
        )
        self.service.log_work(
            message="Implemented follow-up change after handoff.",
            task_id=task["id"],
            files=[str(repo / "app.py")],
            actor="test",
            project_path=str(repo),
        )
        self.service.log_decision(
            title="Prefer delta reads",
            decision="Use delta context for resumed sessions.",
            rationale="It reduces rereading unchanged project history.",
            impact="Faster startup for follow-up work.",
            task_id=task["id"],
            actor="test",
            project_path=str(repo),
        )

        delta = self.service.generate_delta_context(since_handoff_id=handoff["id"], task_id=task["id"], project_path=str(repo))
        self.assertFalse(delta["cached"])
        self.assertIn("Implemented follow-up change after handoff.", delta["markdown"])
        self.assertIn("Prefer delta reads", delta["markdown"])
        self.assertEqual(delta["metadata"]["counts"]["work_logs"], 1)

    def test_sync_context_writes_tiered_and_delta_artifacts(self) -> None:
        task = self.service.create_task(
            title="Sync context artifacts",
            description="Ensure hot/balanced/deep/delta outputs are written.",
            relevant_files=["server/service.py"],
            actor="test",
        )
        self.service.set_current_task(task_id=task["id"], actor="test")
        self.service.log_work(
            message="Prepared context artifact sync verification.",
            task_id=task["id"],
            files=["server/service.py"],
            actor="test",
        )
        sync_result = self.service.sync_context(project_path=str(self.temp_dir))
        files = sync_result["files"]
        self.assertIn("HOT_CONTEXT.md", files)
        self.assertIn("BALANCED_CONTEXT.md", files)
        self.assertIn("DEEP_CONTEXT.md", files)
        self.assertIn("DELTA_CONTEXT.md", files)
        self.assertIn("RETRIEVAL_CONTEXT.md", files)
        self.assertIn("STABLE_CONTEXT.md", files)
        self.assertIn("DYNAMIC_CONTEXT.md", files)
        self.assertIn("prompt_segments.json", files)
        self.assertIn("retrieval_context.json", files)
        self.assertIn("token_usage_stats.json", files)
        self.assertTrue(Path(files["HOT_CONTEXT.md"]).exists())
        self.assertTrue(Path(files["DELTA_CONTEXT.md"]).exists())
        self.assertTrue(Path(files["RETRIEVAL_CONTEXT.md"]).exists())
        self.assertTrue(Path(files["STABLE_CONTEXT.md"]).exists())
        self.assertTrue(Path(files["DYNAMIC_CONTEXT.md"]).exists())

    def test_compact_tool_output_saves_raw_capture_and_metrics(self) -> None:
        output = "\n".join(
            [
                "============================= test session starts =============================",
                "collected 2 items",
                "tests/test_example.py::test_ok PASSED",
                "tests/test_example.py::test_fail FAILED",
                "E   AssertionError: expected 1 == 2",
                "Traceback (most recent call last):",
                "  File \"tests/test_example.py\", line 42, in test_fail",
                "    assert 1 == 2",
            ]
            + [f"debug line {idx}" for idx in range(120)]
        )
        result = self.service.compact_tool_output(
            command="pytest -q",
            output=output,
            exit_code=1,
            actor="test",
            project_path=str(self.temp_dir),
        )

        self.assertEqual(result["profile"], "tests")
        self.assertTrue(result["was_compacted"])
        self.assertLess(result["compact_tokens_est"], result["raw_tokens_est"])
        self.assertIsNotNone(result["raw_capture"])
        capture = self.service.get_raw_output_capture(result["raw_capture"]["capture_id"], include_content=True, project_path=str(self.temp_dir))
        self.assertIsNotNone(capture)
        self.assertIn("AssertionError", capture["content"])

        stats = self.service.get_token_usage_stats(project_path=str(self.temp_dir))
        operations = {item["operation"] for item in stats["by_operation"]}
        self.assertIn("compact_tool_output", operations)
        self.assertGreater(stats["totals"]["saved_tokens"], 0)

    def test_record_command_event_persists_summaries_and_raw_capture(self) -> None:
        task = self.service.create_task(
            title="Bug: Command verification",
            description="Track terminal results without replaying raw output.",
            relevant_files=["server/service.py"],
            tags=["bug"],
            actor="test",
        )
        session = self.service.session_open(
            actor="test-agent",
            client_name="unit-test",
            model_name="test-model",
            project_path=str(self.temp_dir),
            initial_request="Capture a failing command",
            session_goal="Store command history",
            task_id=task["id"],
        )
        stdout = "\n".join([f"collect line {idx}" for idx in range(40)])
        stderr = "\n".join(
            [
                "tests/test_cli.py::test_fail FAILED",
                "E   AssertionError: expected ok",
                "Traceback (most recent call last):",
                "  File \"tests/test_cli.py\", line 14, in test_fail",
            ]
        )

        event = self.service.record_command_event(
            command_text="pytest -q",
            stdout=stdout,
            stderr=stderr,
            exit_code=1,
            duration_ms=1850,
            actor="test-agent",
            session_id=session["id"],
            task_id=task["id"],
            files_changed=["tests/test_cli.py", "server/service.py"],
            project_path=str(self.temp_dir),
        )

        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["exit_code"], 1)
        self.assertEqual(event["output_profile"], "tests")
        self.assertIn("AssertionError", event["stderr_summary"])
        self.assertIn("pytest -q", event["summary"])
        self.assertTrue(event["raw_output_available"])
        self.assertIsNotNone(event["raw_capture"])
        self.assertEqual(event["files_changed"], ["tests/test_cli.py", "server/service.py"])

        loaded = self.service.get_command_event(event["id"], project_path=str(self.temp_dir))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["id"], event["id"])

        latest = self.service.get_last_command_result(session_id=session["id"], project_path=str(self.temp_dir))
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], event["id"])

        failures = self.service.get_command_failures(session_id=session["id"], project_path=str(self.temp_dir))
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["id"], event["id"])

        stats = self.service.get_token_usage_stats(project_path=str(self.temp_dir))
        operations = {item["operation"] for item in stats["by_operation"]}
        self.assertIn("record_command_event", operations)

    def test_command_history_fast_paths_and_delta_context(self) -> None:
        task = self.service.create_task(
            title="Feature: Command history",
            description="Expose recent commands through deterministic fast paths.",
            relevant_files=["server/service.py", "server/store.py"],
            tags=["feature"],
            actor="test",
        )
        session = self.service.session_open(
            actor="test-agent",
            client_name="unit-test",
            model_name="test-model",
            project_path=str(self.temp_dir),
            initial_request="Track command history",
            session_goal="Use fast-path command lookup",
            task_id=task["id"],
        )
        handoff = self.service.create_handoff(
            summary="Baseline before command activity.",
            next_steps="Record command events after this point.",
            task_id=task["id"],
            from_actor="test",
            to_actor="next-agent",
            project_path=str(self.temp_dir),
        )
        self.service.record_command_event(
            command_text="rg TODO server",
            output="server/service.py:123: TODO improve batching\nserver/store.py:456: TODO cleanup\n",
            exit_code=0,
            actor="test-agent",
            session_id=session["id"],
            task_id=task["id"],
            files_changed=["server/service.py", "server/store.py"],
            project_path=str(self.temp_dir),
        )
        failure = self.service.record_command_event(
            command_text="pytest tests/test_service.py -q",
            stderr="FAILED tests/test_service.py::test_example\nAssertionError: boom\nTraceback\n",
            exit_code=1,
            actor="test-agent",
            session_id=session["id"],
            task_id=task["id"],
            project_path=str(self.temp_dir),
        )

        fast_recent = self.service.get_fast_path_response(
            kind="recent_commands",
            session_id=session["id"],
            as_markdown=True,
            project_path=str(self.temp_dir),
        )
        self.assertIn("Fast Path: Recent Commands", fast_recent["markdown"])
        self.assertIn("pytest tests/test_service.py -q", fast_recent["markdown"])

        fast_last = self.service.get_fast_path_response(
            kind="last_command",
            session_id=session["id"],
            project_path=str(self.temp_dir),
        )
        self.assertEqual(fast_last["json"]["id"], failure["id"])

        fast_failures = self.service.get_fast_path_response(
            kind="command_failures",
            session_id=session["id"],
            project_path=str(self.temp_dir),
        )
        self.assertEqual(len(fast_failures["json"]), 1)
        self.assertEqual(fast_failures["json"][0]["id"], failure["id"])

        status = self.service.get_project_status_snapshot(project_path=str(self.temp_dir))
        self.assertTrue(status["recent_commands"])

        delta = self.service.generate_delta_context(
            since_handoff_id=handoff["id"],
            task_id=task["id"],
            project_path=str(self.temp_dir),
        )
        self.assertIn("## Command Activity", delta["markdown"])
        self.assertEqual(delta["metadata"]["counts"]["command_events"], 2)

    def test_hot_write_paths_can_defer_sync(self) -> None:
        task = self.service.create_task(
            title="Feature: Deferred sync",
            description="Avoid heavy sync work on hot command/session writes.",
            actor="test",
        )
        with patch.object(self.service, "_submit_deferred_sync") as submit_sync, patch.object(self.service, "sync_all") as sync_all:
            session = self.service.session_open(
                actor="test-agent",
                client_name="unit-test",
                model_name="test-model",
                project_path=str(self.temp_dir),
                initial_request="Open with deferred sync",
                session_goal="Stay on the fast path",
                task_id=task["id"],
            )
            self.assertEqual(session["sync"]["mode"], "deferred")
            submit_sync.assert_called_once()
            sync_all.assert_not_called()

        with patch.object(self.service, "_submit_deferred_sync") as submit_sync, patch.object(self.service, "sync_all") as sync_all:
            event = self.service.record_command_event(
                command_text="rg TODO server",
                output="server/service.py:10: TODO\n",
                actor="test-agent",
                task_id=task["id"],
                project_path=str(self.temp_dir),
            )
            self.assertEqual(event["sync"]["mode"], "deferred")
            submit_sync.assert_called_once()
            sync_all.assert_not_called()

    def test_command_policy_and_batch_recording(self) -> None:
        task = self.service.create_task(
            title="Feature: Command batches",
            description="Classify and batch safe terminal commands.",
            actor="test",
        )
        session = self.service.session_open(
            actor="test-agent",
            client_name="unit-test",
            model_name="test-model",
            project_path=str(self.temp_dir),
            initial_request="Classify batchable commands",
            session_goal="Store a batch summary",
            task_id=task["id"],
            sync_mode="none",
        )

        read_policy = self.service.get_command_execution_policy(
            command="rg TODO server",
            task_id=task["id"],
            project_path=str(self.temp_dir),
        )
        install_policy = self.service.get_command_execution_policy(
            command="npm install",
            task_id=task["id"],
            project_path=str(self.temp_dir),
        )
        self.assertEqual(read_policy["risk_level"], "low")
        self.assertTrue(read_policy["can_batch"])
        self.assertEqual(install_policy["risk_level"], "high")
        self.assertTrue(install_policy["needs_model_review"])

        batch = self.service.record_command_batch(
            commands=[
                {"command_text": "rg TODO server", "output": "server/service.py:10: TODO\n"},
                {"command_text": "git status", "output": "On branch main\nnothing to commit\n"},
            ],
            actor="test-agent",
            session_id=session["id"],
            task_id=task["id"],
            sync_mode="none",
            project_path=str(self.temp_dir),
        )
        self.assertEqual(batch["command_count"], 2)
        self.assertEqual(batch["risk_counts"]["low"], 2)
        self.assertTrue(all(item["metadata"]["batch_id"] == batch["batch_id"] for item in batch["commands"]))

    def test_startup_context_prefers_command_history_and_cached_delta(self) -> None:
        task = self.service.create_task(
            title="Feature: Startup context",
            description="Resume with delta and recent command summaries.",
            relevant_files=["server/service.py"],
            actor="test",
        )
        session = self.service.session_open(
            actor="test-agent",
            client_name="unit-test",
            model_name="test-model",
            project_path=str(self.temp_dir),
            initial_request="Prepare startup context",
            session_goal="Resume quickly",
            task_id=task["id"],
            sync_mode="none",
        )
        self.service.record_command_event(
            command_text="pytest -q",
            stderr="FAILED tests/test_service.py::test_example\nAssertionError: boom\n",
            exit_code=1,
            actor="test-agent",
            session_id=session["id"],
            task_id=task["id"],
            sync_mode="none",
            project_path=str(self.temp_dir),
        )
        self.service._run_precompute(str(self.temp_dir))

        startup = self.service.generate_startup_context(
            task_id=task["id"],
            session_id=session["id"],
            prefer_cached_delta=True,
            project_path=str(self.temp_dir),
        )
        self.assertTrue(startup["delta_cached"])
        self.assertIn("## Recent Commands", startup["markdown"])
        self.assertIn("## Recent Command Failures", startup["markdown"])
        self.assertIn("## Execution Policy Hint", startup["markdown"])

        fast = self.service.get_fast_path_response(
            kind="startup_context",
            session_id=session["id"],
            task_id=task["id"],
            project_path=str(self.temp_dir),
        )
        self.assertIn("Startup Context", fast["markdown"])

        resume = self.service.generate_resume_packet(task_id=task["id"], project_path=str(self.temp_dir), write_files=False)
        self.assertIn("## Recent Commands", resume["markdown"])
        self.assertIn("## Recent Command Failures", resume["markdown"])

        startup_resource = self.service.get_resource("obsmcp://context/startup", project_path=str(self.temp_dir))
        self.assertIn("Startup Context", startup_resource["text"])

    def test_generate_prompt_segments_and_token_metrics(self) -> None:
        task = self.service.create_task(
            title="Prompt segments",
            description="Verify stable and dynamic prompt segments.",
            relevant_files=["server/service.py"],
            actor="test",
        )
        self.service.set_current_task(task_id=task["id"], actor="test")
        self.service.log_work(
            message="Prepared prompt segment verification.",
            task_id=task["id"],
            files=["server/service.py"],
            actor="test",
        )

        segments = self.service.generate_prompt_segments(task_id=task["id"], project_path=str(self.temp_dir))
        self.assertIn("Stable Prompt Prefix", segments["stable_markdown"])
        self.assertIn("## Architecture", segments["stable_markdown"])
        self.assertIn(task["id"], segments["dynamic_markdown"])
        self.assertIn("## Latest Handoff", segments["dynamic_markdown"])

        self.service.record_token_usage(
            operation="claude_api_call",
            event_type="provider_usage",
            provider="opusmax",
            model_name="claude-opus",
            raw_input_tokens=1200,
            raw_output_tokens=240,
            cache_creation_input_tokens=600,
            cache_read_input_tokens=300,
            project_path=str(self.temp_dir),
            metadata={"source": "unit-test"},
        )
        stats = self.service.get_token_usage_stats(project_path=str(self.temp_dir))
        self.assertGreaterEqual(stats["event_count"], 2)
        self.assertGreater(stats["totals"]["cache_creation_input_tokens"], 0)
        self.assertGreater(stats["totals"]["cache_read_input_tokens"], 0)

        stable_resource = self.service.get_resource("obsmcp://context/stable", project_path=str(self.temp_dir))
        dynamic_resource = self.service.get_resource("obsmcp://context/dynamic", project_path=str(self.temp_dir))
        metrics_resource = self.service.get_resource("obsmcp://metrics/tokens", project_path=str(self.temp_dir))
        self.assertIn("Stable Prompt Prefix", stable_resource["text"])
        self.assertIn(task["id"], dynamic_resource["text"])
        self.assertGreater(metrics_resource["json"]["event_count"], 0)

    def test_list_tools_includes_opusmax_backed_web_and_image_tools(self) -> None:
        tool_names = {tool["name"] for tool in self.service.list_tool_definitions()}
        self.assertIn("web_search", tool_names)
        self.assertIn("understand_image", tool_names)

    def test_generate_startup_prompt_template_appends_output_contract_and_metrics(self) -> None:
        self.config.output_compression.enabled = True
        self.config.output_compression.mode = "prompt_only"
        self.config.output_compression.style = "concise_professional"
        service = ObsmcpService(self.config)

        rendered = service.generate_startup_prompt_template(project_path=str(self.temp_dir))

        self.assertIn("## Response Style Contract", rendered)
        self.assertIn("Start with the answer", rendered)
        stats = service.get_token_usage_stats(project_path=str(self.temp_dir), operation="generate_startup_prompt_template")
        self.assertGreaterEqual(stats["event_count"], 1)

    def test_generate_startup_prompt_template_supports_gateway_enforced_contract(self) -> None:
        self.config.output_compression.enabled = True
        self.config.output_compression.mode = "gateway_enforced"
        service = ObsmcpService(self.config)

        rendered = service.generate_startup_prompt_template(project_path=str(self.temp_dir))

        self.assertIn("## Enforced Response Contract", rendered)
        self.assertIn("Put the answer first.", rendered)

    def test_get_command_execution_policy_includes_output_policy_bypass(self) -> None:
        self.config.output_compression.enabled = True
        self.config.output_compression.mode = "prompt_only"
        service = ObsmcpService(self.config)

        policy = service.get_command_execution_policy(
            command="Remove-Item -LiteralPath build -Recurse -Force",
            project_path=str(self.temp_dir),
        )

        self.assertEqual(policy["action_type"], "destructive")
        self.assertEqual(policy["output_policy"]["mode"], "off")
        self.assertEqual(policy["output_policy"]["bypass_reason"], "destructive_actions")

    def test_get_output_response_policy_honors_task_override(self) -> None:
        self.config.output_compression.enabled = True
        self.config.output_compression.mode = "prompt_only"
        self.config.output_compression.task_overrides["review"] = TaskOutputOverrideConfig(style="terse_technical", level="full")
        service = ObsmcpService(self.config)
        task = service.create_task(
            title="Code review pagination changes",
            description="Review regression-sensitive output behavior.",
            tags=["review"],
            actor="test",
            project_path=str(self.temp_dir),
        )
        service.set_current_task(task_id=task["id"], actor="test", project_path=str(self.temp_dir))

        policy = service.call_tool(
            "get_output_response_policy",
            {
                "task_id": task["id"],
                "operation_kind": "review",
                "project_path": str(self.temp_dir),
            },
        )

        self.assertEqual(policy["mode"], "prompt_only")
        self.assertEqual(policy["style"], "terse_technical")
        self.assertEqual(policy["task_type"], "review")
        self.assertIn("findings and risks", policy["prompt_contract"])

    def test_web_search_records_provider_usage(self) -> None:
        class FakeToolProvider:
            def web_search(self, query: str, max_results: int | None = None) -> dict[str, Any]:
                return {
                    "request_id": "ws_test123",
                    "provider": "opusmax",
                    "endpoint": "/tools/web_search",
                    "query": query,
                    "latency_ms": 12.5,
                    "results": [{"title": "obsmcp"}],
                    "summary": "result ok",
                    "raw": {"results": [{"title": "obsmcp"}]},
                }

        with patch("server.service.get_opusmax_tool_provider", return_value=FakeToolProvider()):
            result = self.service.web_search(
                query="obsmcp",
                actor="test",
                session_id="SESSION-1",
                task_id="TASK-1",
                client_name="unit-test",
                project_path=str(self.temp_dir),
            )

        self.assertTrue(result["request_id"].startswith("ws_"))
        self.assertEqual(result["provider"], "opusmax")
        self.assertEqual(result["results"][0]["title"], "obsmcp")
        stats = self.service.get_token_usage_stats(project_path=str(self.temp_dir), operation="web_search")
        self.assertGreaterEqual(stats["event_count"], 1)

    def test_understand_image_records_provider_usage(self) -> None:
        class FakeToolProvider:
            def understand_image(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "request_id": "img_test123",
                    "provider": "opusmax",
                    "endpoint": "/tools/understand_image",
                    "prompt": kwargs["prompt"],
                    "latency_ms": 9.1,
                    "image_source": {"kind": "url"},
                    "analysis": "image ok",
                    "raw": {"analysis": "image ok"},
                }

        with patch("server.service.get_opusmax_tool_provider", return_value=FakeToolProvider()):
            result = self.service.understand_image(
                prompt="Describe this image",
                image_url="https://example.com/test.png",
                actor="test",
                session_id="SESSION-2",
                task_id="TASK-2",
                client_name="unit-test",
                project_path=str(self.temp_dir),
            )

        self.assertTrue(result["request_id"].startswith("img_"))
        self.assertEqual(result["provider"], "opusmax")
        self.assertEqual(result["analysis"], "image ok")
        stats = self.service.get_token_usage_stats(project_path=str(self.temp_dir), operation="understand_image")
        self.assertGreaterEqual(stats["event_count"], 1)

    def test_retrieval_context_fast_paths_and_resume_targeting(self) -> None:
        task = self.service.create_task(
            title="Bug: Prompt cache regression",
            description="Investigate cache misses in prompt assembly.",
            relevant_files=["server/service.py", "server/store.py"],
            tags=["bug"],
            actor="test",
        )
        self.service.set_current_task(task_id=task["id"], actor="test")
        self.service.log_work(
            message="Investigated prompt cache misses in segment assembly.",
            task_id=task["id"],
            files=["server/service.py"],
            actor="test",
        )
        self.service.log_decision(
            title="Bias bug retrieval",
            decision="Show blockers and recent work before semantic hints for bug tasks.",
            rationale="Bug investigations need current failure context first.",
            impact="Faster debugging startup.",
            task_id=task["id"],
            actor="test",
        )

        retrieval = self.service.generate_retrieval_context(
            query="prompt cache misses",
            task_id=task["id"],
            project_path=str(self.temp_dir),
        )
        self.assertIn("Retrieval Context", retrieval["markdown"])
        self.assertIn(task["id"], retrieval["markdown"])
        self.assertIn("Ranked Recent Work", retrieval["markdown"])

        fast = self.service.get_fast_path_response(
            kind="current_task",
            task_id=task["id"],
            as_markdown=True,
            project_path=str(self.temp_dir),
        )
        self.assertEqual(fast["source"], "deterministic")
        self.assertIn(task["id"], fast["markdown"])

        status_fast = self.service.get_fast_path_response(kind="project_status", project_path=str(self.temp_dir))
        self.assertEqual(status_fast["json"]["current_task"]["id"], task["id"])

        resume = self.service.generate_resume_packet(task_id=task["id"], project_path=str(self.temp_dir), write_files=False)
        self.assertIn("## Targeted Retrieval", resume["markdown"])
        self.assertIn("Matched Files:", resume["markdown"])

        retrieval_resource = self.service.get_resource("obsmcp://context/retrieval", project_path=str(self.temp_dir))
        self.assertIn("Retrieval Context", retrieval_resource["text"])

    def test_chunk_navigation_and_progressive_context(self) -> None:
        task = self.service.create_task(
            title="Feature: Progressive context",
            description="Verify chunk navigation for prompt segments and retrieval context.",
            relevant_files=["server/service.py", "tests/test_service.py", "docs/ARCHITECTURE.md"],
            tags=["feature"],
            actor="test",
        )
        self.service.set_current_task(task_id=task["id"], actor="test")
        self.service.log_work(
            message="Prepared multi-section context for chunk navigation.",
            task_id=task["id"],
            files=["server/service.py", "tests/test_service.py"],
            actor="test",
        )

        chunk_plan = self.service.list_context_chunks(
            artifact_type="prompt_segments",
            profile="balanced",
            task_id=task["id"],
            project_path=str(self.temp_dir),
        )
        self.assertGreaterEqual(chunk_plan["total_chunks"], 1)
        self.assertTrue(chunk_plan["chunks"])

        first_chunk = self.service.retrieve_context_chunk(
            artifact_type="prompt_segments",
            chunk_index=0,
            profile="balanced",
            task_id=task["id"],
            project_path=str(self.temp_dir),
        )
        self.assertIn("markdown", first_chunk)
        if first_chunk["is_last"]:
            self.assertIsNone(first_chunk["next_chunk_index"])
        else:
            self.assertIsNotNone(first_chunk["next_chunk_index"])

        progressive = self.service.generate_progressive_context(
            artifact_type="retrieval_context",
            profile="balanced",
            start_chunk=0,
            chunk_count=1,
            task_id=task["id"],
            query="progressive context",
            project_path=str(self.temp_dir),
        )
        self.assertEqual(progressive["chunk_count"], 1)
        self.assertIn("combined_markdown", progressive)

    def test_optimization_policy_adapts_bug_debug_compaction(self) -> None:
        task = self.service.create_task(
            title="Bug: Test output overflow",
            description="Need more failure detail in debug mode.",
            relevant_files=["server/service.py"],
            tags=["bug"],
            actor="test",
        )
        self.service.set_current_task(task_id=task["id"], actor="test")

        compact_policy = self.service.get_optimization_policy(
            mode="compact",
            task_id=task["id"],
            command="pytest -q",
            exit_code=1,
            project_path=str(self.temp_dir),
        )
        debug_policy = self.service.get_optimization_policy(
            mode="debug",
            task_id=task["id"],
            command="pytest -q",
            exit_code=1,
            project_path=str(self.temp_dir),
        )
        self.assertEqual(compact_policy["task_type"], "bug")
        self.assertGreater(debug_policy["window_scale"], compact_policy["window_scale"])
        self.assertTrue(debug_policy["raw_capture_on_failure"])

        output = "\n".join([f"line {idx}" for idx in range(80)] + ["FAILED", "Traceback", "AssertionError"])
        compact_result = self.service.compact_tool_output(
            command="pytest -q",
            output=output,
            exit_code=1,
            policy_mode="compact",
            task_id=task["id"],
            actor="test",
            project_path=str(self.temp_dir),
        )
        debug_result = self.service.compact_tool_output(
            command="pytest -q",
            output=output,
            exit_code=1,
            policy_mode="debug",
            task_id=task["id"],
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.assertGreaterEqual(debug_result["compact_lines"], compact_result["compact_lines"])
        self.assertEqual(debug_result["policy_mode"], "debug")

    def test_scan_codebase_ignores_generated_runtime_artifacts(self) -> None:
        (self.temp_dir / "app.py").write_text(
            "def hello() -> str:\n    return 'world'\n",
            encoding="utf-8",
        )
        first = self.service.scan_codebase(project_path=str(self.temp_dir), force_refresh=True)
        self.assertEqual(first["status"], "generated")

        (self.temp_dir / "projects" / "generated-project" / "vault").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "projects" / "generated-project" / "vault" / "note.md").write_text("# generated\n", encoding="utf-8")
        (self.temp_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "logs" / "obsmcp.log").write_text("runtime log\n", encoding="utf-8")
        (self.temp_dir / "data").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "data" / "obsmcp.pid").write_text("1234\n", encoding="utf-8")
        (self.temp_dir / ".context").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / ".context" / "PROJECT_CONTEXT.md").write_text("# context\n", encoding="utf-8")
        (self.temp_dir / "obsidian" / "vault" / "Research").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "obsidian" / "vault" / "Research" / "Generated.md").write_text("# generated note\n", encoding="utf-8")

        second = self.service.scan_codebase(project_path=str(self.temp_dir), force_refresh=False)
        self.assertEqual(second["status"], "current")

    def test_background_scan_job_polling(self) -> None:
        repo = self.temp_dir / "job-repo"
        repo.mkdir()
        (repo / "app.py").write_text("def greet() -> str:\n    return 'hi'\n", encoding="utf-8")

        queued = self.service.call_tool("scan_codebase", {"project_path": str(repo), "force_refresh": True})
        self.assertIn(queued["status"], {"queued", "running"})
        self.assertTrue(queued["id"].startswith("SCAN-"))

        finished = self.service.wait_for_scan_job(queued["id"], project_path=str(repo), wait_seconds=30)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["result"]["status"], "generated")

        current = self.service.call_tool("scan_codebase", {"project_path": str(repo), "force_refresh": False})
        self.assertEqual(current["status"], "current")

    def test_session_audit_and_close_flow(self) -> None:
        task = self.service.create_task(
            title="Audit continuity",
            description="Make sure session policy is enforced.",
            actor="test",
        )
        session = self.service.session_open(
            actor="test-agent",
            client_name="unit-test",
            model_name="test-model",
            project_path=str(self.temp_dir),
            initial_request="Inspect the project and log findings.",
            session_goal="Verify session tracking.",
            task_id=task["id"],
            heartbeat_interval_seconds=30,
            work_log_interval_seconds=30,
        )
        self.assertTrue(session["id"].startswith("SESSION-"))

        issues = self.service.detect_missing_writeback()
        self.assertEqual(issues, [])

        self.service.session_heartbeat(
            session_id=session["id"],
            actor="test-agent",
            status_note="Still exploring the codebase.",
            task_id=task["id"],
            files=["server/store.py"],
            create_work_log=True,
        )
        self.assertEqual(self.service.detect_missing_writeback(), [])

        closed = self.service.session_close(
            session_id=session["id"],
            actor="test-agent",
            summary="Session completed with a clean handoff.",
            create_handoff=True,
            handoff_summary="Next agent can continue from the synced files.",
            handoff_next_steps="Read the handoff and continue implementation.",
            handoff_to_actor="next-agent",
        )
        self.assertEqual(closed["status"], "closed")
        project_paths = self.service.get_project_workspace_paths(project_path=str(self.temp_dir))
        audit_path = Path(project_paths["context_path"]) / "SESSION_AUDIT.json"
        self.assertTrue(audit_path.exists())
        self.assertEqual(json.loads(audit_path.read_text(encoding="utf-8")), [])
        session_dir = Path(project_paths["sessions_path"]) / session["id"]
        self.assertTrue((session_dir / "metadata.json").exists())
        self.assertTrue((session_dir / "heartbeat.jsonl").exists())
        self.assertTrue((session_dir / "worklog.md").exists())
        self.assertTrue((session_dir / "handoff.md").exists())

    def test_session_open_auto_resumes_matching_session(self) -> None:
        first = self.service.session_open(
            actor="codex",
            client_name="vscode-codex",
            model_name="gpt-5",
            project_path=str(self.temp_dir),
            initial_request="First open",
            session_goal="Start work",
        )
        second = self.service.session_open(
            actor="codex",
            client_name="vscode-codex",
            model_name="gpt-5",
            project_path=str(self.temp_dir),
            initial_request="Reopen work",
            session_goal="Resume work",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["resumed"])
        self.assertEqual(len(self.service.get_active_sessions(project_path=str(self.temp_dir))["sessions"]), 1)

    def test_session_open_derives_readable_label_workstream_and_normalizes_identity(self) -> None:
        session = self.service.session_open(
            actor="claude-code",
            client_name="claude code",
            model_name="opus-4.6",
            project_path=str(self.temp_dir),
            initial_request="This is a task for the managing director's email.",
            session_goal="Prepare the final draft for leadership review.",
            resume_strategy="new",
        )

        self.assertEqual(session["session_label"], "Managing Director's Email")
        self.assertEqual(session["workstream_key"], "managing-directors-email")
        self.assertEqual(session["workstream_title"], "Managing Director's Email")
        self.assertEqual(session["client_name"], "claude-code-vscode")
        self.assertEqual(session["model_name"], "claude-opus-4-6")

        health = self.service.health_check(project_path=str(self.temp_dir))
        self.assertEqual(health["active_sessions"], 1)

    def test_session_open_uses_mismatch_guard_for_same_workstream_different_task(self) -> None:
        task_a = self.service.create_task(
            title="Managing director email draft",
            description="Prepare the first draft.",
            actor="test",
            project_path=str(self.temp_dir),
        )
        task_b = self.service.create_task(
            title="Managing director email review",
            description="Prepare the final review packet.",
            actor="test",
            project_path=str(self.temp_dir),
        )

        first = self.service.session_open(
            actor="claude-code",
            client_name="claude-code",
            model_name="claude-opus-4.6",
            project_path=str(self.temp_dir),
            initial_request="Draft the managing director email.",
            session_goal="Finish the initial version.",
            task_id=task_a["id"],
            workstream_key="managing-director-email",
            session_label="Managing Director Email - Draft",
        )
        second = self.service.session_open(
            actor="claude-code",
            client_name="claude-code",
            model_name="claude-opus-4.6",
            project_path=str(self.temp_dir),
            initial_request="Review the managing director email for final approval.",
            session_goal="Ship the reviewed version.",
            task_id=task_b["id"],
            workstream_key="managing-director-email",
            session_label="Managing Director Email - Final Review",
        )

        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second.get("resumed", False))
        self.assertTrue(any("Auto-resume skipped" in warning for warning in second["warnings"]))

    def test_startup_preflight_reports_done_task_taskless_session_and_handoff_mismatch(self) -> None:
        task = self.service.create_task(
            title="Old implementation task",
            description="Already completed work.",
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(self.temp_dir))
        self.service.update_task(task_id=task["id"], status="done", actor="test", project_path=str(self.temp_dir))
        other_task = self.service.create_task(
            title="Different active task",
            description="Used to trigger handoff mismatch.",
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.service.create_handoff(
            summary="Completed prior work.",
            next_steps="No more changes required.",
            task_id=other_task["id"],
            from_actor="test",
            to_actor="next-agent",
            project_path=str(self.temp_dir),
        )

        preflight = self.service.get_startup_preflight(
            actor="claude-code",
            task_id=task["id"],
            initial_request="Create a complete beginner documentation for the Beanav ERP system with real-world examples.",
            session_goal="Write the full documentation from scratch for class 8 students.",
            project_path=str(self.temp_dir),
        )
        taskless_preflight = self.service.get_startup_preflight(
            actor="claude-code",
            initial_request="Create a complete beginner documentation for the Beanav ERP system with real-world examples.",
            session_goal="Write the full documentation from scratch for class 8 students.",
            project_path=str(self.temp_dir),
        )

        warning_codes = {item["code"] for item in preflight["warnings"]}
        self.assertIn("current_task_done", warning_codes)
        self.assertIn("latest_handoff_task_mismatch", warning_codes)
        self.assertFalse(preflight["ok"])
        self.assertIn("Create or select the correct task", preflight["recommended_action"])
        self.assertIn("session_without_task", {item["code"] for item in taskless_preflight["warnings"]})
        self.assertIn("Create or select", taskless_preflight["recommended_action"])

    def test_resume_board_surfaces_paused_and_stale_workstreams(self) -> None:
        current_task = self.service.create_task(
            title="ERP documentation",
            description="Active writing task.",
            actor="test",
            project_path=str(self.temp_dir),
        )
        paused_task = self.service.create_task(
            title="Finance approval memo",
            description="Paused with no live session.",
            actor="test",
            project_path=str(self.temp_dir),
        )
        stale_task = self.service.create_task(
            title="Managing director email",
            description="Open session has gone stale.",
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.service.set_current_task(task_id=current_task["id"], actor="test", project_path=str(self.temp_dir))

        current_session = self.service.session_open(
            actor="claude-code",
            client_name="claude-code",
            model_name="opus-4.6",
            project_path=str(self.temp_dir),
            initial_request="Continue ERP documentation.",
            session_goal="Write the architecture chapter.",
            task_id=current_task["id"],
            resume_strategy="new",
        )
        stale_session = self.service.session_open(
            actor="claude-code",
            client_name="claude-code",
            model_name="opus-4.6",
            project_path=str(self.temp_dir),
            initial_request="Resume the managing director email.",
            session_goal="Finish the paused email draft.",
            task_id=stale_task["id"],
            session_label="Managing Director Email",
            workstream_key="managing-director-email",
            resume_strategy="new",
        )
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        store = self.service._store(str(self.temp_dir))
        with store._connect() as connection:
            connection.execute("UPDATE sessions SET heartbeat_at = ?, opened_at = ? WHERE id = ?", (stale_time, stale_time, stale_session["id"]))
            connection.commit()

        self.service.create_handoff(
            summary="Architecture outline completed.",
            next_steps="Write module-by-module details.",
            task_id=current_task["id"],
            from_actor="test",
            to_actor="next-agent",
            project_path=str(self.temp_dir),
        )

        board = self.service.get_resume_board(project_path=str(self.temp_dir))

        self.assertEqual(board["current_task"]["id"], current_task["id"])
        self.assertIn(paused_task["id"], {item["id"] for item in board["paused_tasks"]})
        self.assertIn(stale_session["id"], {item["id"] for item in board["stale_sessions"]})
        self.assertEqual(board["recommended_resume_target"]["task"]["id"], current_task["id"])
        self.assertEqual(board["recommended_resume_target"]["session"]["id"], current_session["id"])
        self.assertTrue(board["latest_handoffs"])

    def test_reset_project_returns_post_reset_snapshot(self) -> None:
        task = self.service.create_task(
            title="Reset verification task",
            description="Create state and wipe it clean.",
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(self.temp_dir))
        self.service.log_work(
            message="Created work that should disappear after reset.",
            task_id=task["id"],
            actor="test",
            project_path=str(self.temp_dir),
        )
        session = self.service.session_open(
            actor="claude-code",
            client_name="claude-code",
            model_name="opus-4.6",
            project_path=str(self.temp_dir),
            initial_request="Open session before reset.",
            session_goal="Verify cleanup.",
            task_id=task["id"],
            resume_strategy="new",
        )
        self.assertTrue(session["id"].startswith("SESSION-"))

        result = self.service.reset_project(scope="full", actor="test", project_path=str(self.temp_dir))
        snapshot = result["post_reset_snapshot"]

        self.assertEqual(snapshot["current_task"], None)
        self.assertEqual(snapshot["active_tasks"], [])
        self.assertEqual(snapshot["latest_handoff"], None)
        self.assertEqual(snapshot["recent_work"], [])
        self.assertEqual(snapshot["active_sessions"], [])

    def test_compatibility_and_incremental_recent_reads(self) -> None:
        task = self.service.create_task(
            title="Compatibility verification",
            description="Check versioning and incremental reads.",
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.service.log_work(
            message="First entry.",
            task_id=task["id"],
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.service.log_work(
            message="Second entry.",
            task_id=task["id"],
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.service.log_decision(
            title="First decision",
            decision="Keep compatibility explicit.",
            rationale="It prevents silent client/server drift.",
            impact="Safer startup checks.",
            task_id=task["id"],
            actor="test",
            project_path=str(self.temp_dir),
        )
        self.service.log_decision(
            title="Second decision",
            decision="Expose version info through a dedicated endpoint.",
            rationale="The client can validate expectations before startup.",
            impact="Fewer runtime mismatches.",
            task_id=task["id"],
            actor="test",
            project_path=str(self.temp_dir),
        )

        recent_work = self.service.get_recent_work(limit=2, project_path=str(self.temp_dir))
        self.assertEqual(len(recent_work), 2)
        older_work = self.service.get_recent_work(after_id=recent_work[-1]["id"], project_path=str(self.temp_dir))
        self.assertTrue(all(item["id"] < recent_work[-1]["id"] for item in older_work))

        decisions = self.service.get_decisions(limit=1, project_path=str(self.temp_dir))
        self.assertEqual(len(decisions), 1)
        older_decisions = self.service.get_decisions(after_id=decisions[-1]["id"], project_path=str(self.temp_dir))
        self.assertTrue(all(item["id"] < decisions[-1]["id"] for item in older_decisions))

        capabilities = self.service.get_server_capabilities(project_path=str(self.temp_dir))
        compatibility = self.service.check_client_compatibility(
            client_api_version=self.service.API_VERSION,
            client_tool_schema_version=self.service.TOOL_SCHEMA_VERSION,
            client_name="claude-code",
            model_name="opus-4.6",
            project_path=str(self.temp_dir),
        )
        mismatch = self.service.check_client_compatibility(
            client_api_version="2025.01.01",
            client_tool_schema_version=999,
            client_name="claude-code",
            model_name="opus-4.6",
            project_path=str(self.temp_dir),
        )

        self.assertTrue(capabilities["features"]["resume_board"])
        self.assertTrue(compatibility["compatible"])
        self.assertEqual(compatibility["client"]["client_name"], "claude-code-vscode")
        self.assertEqual(compatibility["client"]["model_name"], "claude-opus-4-6")
        self.assertFalse(mismatch["compatible"])
        self.assertEqual(len(mismatch["warnings"]), 2)

    def test_call_tool_infers_project_from_file_paths(self) -> None:
        repo = self.temp_dir / "infer-repo"
        repo.mkdir()
        source_file = repo / "src" / "module.py"
        source_file.parent.mkdir()
        source_file.write_text("print('hello')\n", encoding="utf-8")
        self.service.register_project(repo_path=str(repo), name="Infer Repo")

        self.service.call_tool(
            "log_work",
            {
                "actor": "test-agent",
                "message": "Project inferred from absolute file path.",
                "files": [str(source_file)],
            },
        )

        snapshot = self.service.get_project_status_snapshot(project_path=str(repo))
        self.assertEqual(snapshot["recent_work"][0]["message"], "Project inferred from absolute file path.")

    def test_call_tool_infers_project_from_cwd_for_session_open(self) -> None:
        repo = self.temp_dir / "cwd-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        nested = repo / "src"
        nested.mkdir()

        with patch.object(self.service, "_registered_project_for_path_hint", side_effect=lambda hint: str(repo) if hint else None):
            with patch("server.service.os.getcwd", return_value=str(nested)):
                with patch.dict("server.service.os.environ", {}, clear=True):
                    session = self.service.call_tool(
                        "session_open",
                        {
                            "actor": "test-agent",
                            "client_name": "unit-test",
                            "model_name": "test-model",
                            "initial_request": "Infer project from cwd",
                            "session_goal": "Auto-route from current repo",
                        },
                    )

        self.assertEqual(session["project_path"], str(repo))

    def test_call_tool_rejects_unscoped_continuity_request(self) -> None:
        unknown = self.temp_dir / "unknown-cwd"
        unknown.mkdir()

        with patch("server.service.os.getcwd", return_value=str(unknown)):
            with patch.dict("server.service.os.environ", {}, clear=True):
                with self.assertRaises(ValueError) as exc:
                    self.service.call_tool(
                        "log_work",
                        {
                            "actor": "test-agent",
                            "message": "This should not route to the default project.",
                        },
                    )

        self.assertIn("Project context is required", str(exc.exception))

    def test_resolve_active_project_from_ide_metadata_workspace_and_active_file(self) -> None:
        repo = self.temp_dir / "ide-repo"
        repo.mkdir()
        source_file = repo / "src" / "main.py"
        source_file.parent.mkdir()
        source_file.write_text("print('hi')\n", encoding="utf-8")

        with patch.object(self.service, "_registered_project_for_path_hint", side_effect=lambda hint: str(repo) if hint else None):
            result = self.service.call_tool(
                "resolve_active_project",
                {
                    "ide_name": "claude",
                    "workspace_folders": [str(repo)],
                    "active_file": str(source_file),
                },
            )

        self.assertTrue(result["resolved"])
        self.assertFalse(result["already_registered"])
        self.assertEqual(result["project_path"], str(repo))
        self.assertEqual(result["resolution_source"], "workspace_folders[0]")
        self.assertEqual(result["ide_name"], "claude")

    def test_resolve_active_project_from_ide_metadata_requires_registration_when_disabled(self) -> None:
        repo = self.temp_dir / "unregistered-ide-repo"
        repo.mkdir()
        active_file = repo / "app.py"
        active_file.write_text("print('x')\n", encoding="utf-8")

        with patch.object(self.service, "_registered_project_for_path_hint", side_effect=lambda hint: str(repo) if hint else None):
            result = self.service.call_tool(
                "resolve_active_project",
                {
                    "active_file": str(active_file),
                    "auto_register": False,
                },
            )

        self.assertFalse(result["resolved"])
        self.assertTrue(result["requires_registration"])
        self.assertEqual(result["project_path"], str(repo))

    def test_resolve_active_project_from_ide_metadata_returns_unresolved_without_hints(self) -> None:
        result = self.service.call_tool(
            "resolve_active_project",
            {
                "ide_name": "claude",
                "auto_register": False,
            },
        )

        self.assertFalse(result["resolved"])
        self.assertEqual(result["project_path"], None)
        self.assertIn("Pass project_path", result["recommended_action"])

    def test_detect_missing_writeback_flags_stale_and_abandoned_sessions(self) -> None:
        session = self.service.session_open(
            actor="test-agent",
            client_name="unit-test",
            model_name="test-model",
            project_path=str(self.temp_dir),
            initial_request="Open stale session",
            session_goal="Leave it idle",
            heartbeat_interval_seconds=30,
            work_log_interval_seconds=30,
        )
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        store = self.service._store(str(self.temp_dir))
        with store._connect() as connection:
            connection.execute(
                "UPDATE sessions SET heartbeat_at = ?, opened_at = ? WHERE id = ?",
                (stale_time, stale_time, session["id"]),
            )
            connection.commit()

        issues = self.service.detect_missing_writeback(project_path=str(self.temp_dir))
        issue_types = {item["issue"] for item in issues if item["session_id"] == session["id"]}
        self.assertIn("stale_open_session", issue_types)
        self.assertIn("abandoned_session", issue_types)

    def test_session_close_enriches_handoff(self) -> None:
        repo = self.temp_dir / "handoff-repo"
        repo.mkdir()
        source_file = repo / "app.py"
        source_file.write_text("def run() -> str:\n    return 'ok'\n", encoding="utf-8")
        self.service.scan_codebase(project_path=str(repo), force_refresh=True)
        task = self.service.create_task(
            title="Close with enriched handoff",
            description="Verify handoff quality defaults.",
            relevant_files=[str(source_file)],
            actor="test",
            project_path=str(repo),
        )
        session = self.service.session_open(
            actor="test-agent",
            client_name="unit-test",
            model_name="test-model",
            project_path=str(repo),
            initial_request="Implement and stop cleanly.",
            session_goal="Leave a useful handoff.",
            task_id=task["id"],
        )
        self.service.log_work(
            message="Implemented the run helper.",
            task_id=task["id"],
            actor="test-agent",
            session_id=session["id"],
            files=[str(source_file)],
            project_path=str(repo),
        )

        closed = self.service.session_close(
            session_id=session["id"],
            actor="test-agent",
            summary="Implemented the run helper.",
            project_path=str(repo),
        )
        self.assertEqual(closed["status"], "closed")
        handoff = self.service.get_latest_handoff(project_path=str(repo))
        self.assertIsNotNone(handoff)
        self.assertIn("Relevant files:", handoff["note"])
        self.assertIn("Recommended semantic lookups:", handoff["note"])
        self.assertTrue(handoff["next_steps"])

    def test_multiple_projects_keep_state_separate(self) -> None:
        project_a = self.temp_dir / "project-a"
        project_b = self.temp_dir / "project-b"
        project_a.mkdir()
        project_b.mkdir()

        task_a = self.service.create_task(
            title="Project A task",
            description="Track project A only.",
            relevant_files=[str(project_a / "src" / "a.py")],
            actor="test",
            project_path=str(project_a),
        )
        task_b = self.service.create_task(
            title="Project B task",
            description="Track project B only.",
            relevant_files=[str(project_b / "src" / "b.py")],
            actor="test",
            project_path=str(project_b),
        )

        self.service.set_current_task(task_id=task_a["id"], actor="test", project_path=str(project_a))
        self.service.set_current_task(task_id=task_b["id"], actor="test", project_path=str(project_b))

        session_a = self.service.call_tool(
            "session_open",
            {
                "actor": "test-agent",
                "client_name": "unit-test",
                "model_name": "test-model",
                "project_path": str(project_a),
                "task_id": task_a["id"],
            },
        )
        self.service.call_tool(
            "log_work",
            {
                "actor": "test-agent",
                "session_id": session_a["id"],
                "task_id": task_a["id"],
                "message": "Investigated project A bug.",
                "files": [str(project_a / "src" / "a.py")],
            },
        )
        self.service.log_work(
            message="Implemented project B feature.",
            task_id=task_b["id"],
            files=[str(project_b / "src" / "b.py")],
            actor="test",
            project_path=str(project_b),
        )

        snapshot_a = self.service.get_project_status_snapshot(project_path=str(project_a))
        snapshot_b = self.service.get_project_status_snapshot(project_path=str(project_b))

        self.assertEqual(snapshot_a["current_task"]["id"], task_a["id"])
        self.assertEqual(snapshot_b["current_task"]["id"], task_b["id"])
        self.assertEqual([task["id"] for task in snapshot_a["active_tasks"]], [task_a["id"]])
        self.assertEqual([task["id"] for task in snapshot_b["active_tasks"]], [task_b["id"]])
        self.assertEqual(snapshot_a["relevant_files"], [str(project_a / "src" / "a.py")])
        self.assertEqual(snapshot_b["relevant_files"], [str(project_b / "src" / "b.py")])

        paths_a = self.service.get_project_workspace_paths(project_path=str(project_a))
        paths_b = self.service.get_project_workspace_paths(project_path=str(project_b))
        self.assertTrue((Path(paths_a["context_path"]) / "CURRENT_TASK.json").exists())
        self.assertTrue((Path(paths_b["context_path"]) / "CURRENT_TASK.json").exists())
        self.assertTrue((Path(paths_a["vault_path"]) / "Projects" / "Project Brief.md").exists())
        self.assertTrue((Path(paths_b["vault_path"]) / "Projects" / "Project Brief.md").exists())

    def test_registry_resume_recovery_and_hub_sync(self) -> None:
        repo = self.temp_dir / "sample-repo"
        repo.mkdir()
        (repo / ".context").mkdir()
        (repo / "obsidian" / "vault" / "Projects").mkdir(parents=True)
        (repo / ".context" / "PROJECT_CONTEXT.md").write_text("# Legacy Context\n", encoding="utf-8")
        (repo / "obsidian" / "vault" / "Projects" / "Legacy.md").write_text("# Legacy Note\n", encoding="utf-8")

        registration = self.service.register_project(repo_path=str(repo), name="Sample Repo", tags=["python", "api"])
        self.assertEqual(registration["name"], "Sample Repo")
        bridge_file = repo / ".obsmcp-link.json"
        self.assertTrue(bridge_file.exists())
        migrated = self.service.migrate_project_layout(project_path=str(repo))
        self.assertGreaterEqual(migrated["copied_count"], 1)

        task = self.service.create_task(
            title="Recoverable task",
            description="Exercise resume and recovery flow.",
            actor="test",
            project_path=str(repo),
        )
        session = self.service.session_open(
            actor="codex",
            client_name="unit-test",
            model_name="test-model",
            project_path=str(repo),
            initial_request="Start implementation",
            session_goal="Leave enough state for recovery",
            task_id=task["id"],
        )
        self.service.log_work(
            message="Implemented the first half of the feature.",
            task_id=task["id"],
            actor="codex",
            session_id=session["id"],
            project_path=str(repo),
        )

        resume = self.service.generate_resume_packet(session_id=session["id"], project_path=str(repo))
        self.assertIn("Resume Packet", resume["markdown"])
        self.assertTrue(Path(resume["path"]).exists())

        recovered = self.service.recover_session(session_id=session["id"], actor="claude", project_path=str(repo))
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["handoff"]["to_actor"], "next-agent")

        hub = self.service.sync_hub()
        self.assertTrue(any(path.endswith("Projects Overview.md") for path in hub["files"]))
        self.assertGreaterEqual(len(self.service.list_projects()), 1)

    def test_semantic_knowledge_flow(self) -> None:
        repo = self.temp_dir / "semantic-repo"
        repo.mkdir()
        (repo / "app.py").write_text(
            """
\"\"\"Application entry helpers.\"\"\"

class ExampleService:
    \"\"\"Coordinates semantic behavior for tests.\"\"\"

    def run(self, task_name: str) -> str:
        \"\"\"Run the named task.\"\"\"
        return task_name.upper()


def helper(task_name: str) -> str:
    \"\"\"Format a task label.\"\"\"
    return f"task:{task_name}"
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (repo / "other.py").write_text(
            """
def helper(task_name: str) -> str:
    return task_name.lower()
""".strip()
            + "\n",
            encoding="utf-8",
        )

        atlas = self.service.scan_codebase(project_path=str(repo), force_refresh=True)
        self.assertEqual(atlas["status"], "generated")
        self.assertGreater(atlas["semantic_index"]["entity_count"], 0)

        task = self.service.create_task(
            title="Semantic task",
            description="Exercise semantic descriptions.",
            relevant_files=["app.py", "other.py"],
            actor="test",
            project_path=str(repo),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(repo))

        module = self.service.describe_module("app.py", project_path=str(repo))
        self.assertEqual(module["entity_type"], "module")
        self.assertIn("app.py", module["file"])

        symbol = self.service.describe_symbol("ExampleService", module_path="app.py", entity_type="class", project_path=str(repo))
        self.assertEqual(symbol["entity_type"], "class")
        self.assertIn("why_it_exists", symbol)

        method = self.service.describe_symbol("run", module_path="app.py", entity_type="function", project_path=str(repo))
        self.assertEqual(method["entity_type"], "function")
        self.assertIn("ExampleService", method["signature"])
        self.assertIn("why_it_exists", method)

        ambiguous = self.service.describe_symbol("helper", entity_type="function", project_path=str(repo))
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(len(ambiguous["candidates"]), 2)

        feature = self.service.describe_feature("Python", project_path=str(repo))
        self.assertEqual(feature["entity_type"], "feature")

        search = self.service.search_code_knowledge("helper", project_path=str(repo))
        self.assertGreaterEqual(search["match_count"], 1)

        related = self.service.get_related_symbols(entity_key=symbol["entity_key"], project_path=str(repo))
        self.assertGreaterEqual(len(related["related_symbols"]), 1)

        cached_before = self.service.describe_module("app.py", project_path=str(repo))
        self.assertTrue(cached_before["cached"])

        (repo / "app.py").write_text(
            """
\"\"\"Application entry helpers updated.\"\"\"

class ExampleService:
    def run(self, task_name: str) -> str:
        return f"RUN:{task_name.upper()}"


def helper(task_name: str) -> str:
    return f"task:{task_name}:updated"
""".strip()
            + "\n",
            encoding="utf-8",
        )

        refreshed = self.service.refresh_semantic_description(module_path="app.py", project_path=str(repo))
        self.assertEqual(refreshed["entity_type"], "module")
        self.assertFalse(refreshed["cached"])

        resume = self.service.generate_resume_packet(task_id=task["id"], project_path=str(repo))
        self.assertIn("Recommended Semantic Lookups", resume["markdown"])

        workspace = self.service.get_project_workspace_paths(project_path=str(repo))
        vault_dir = Path(workspace["vault_path"])
        self.assertTrue((vault_dir / "Research" / "Architecture Map.md").exists())
        self.assertTrue((vault_dir / "Research" / "Module Summaries.md").exists())
        self.assertTrue((vault_dir / "Research" / "Feature Map.md").exists())
        symbol_notes = list((vault_dir / "Research" / "Symbol Knowledge").glob("*.md"))
        self.assertTrue(symbol_notes)

        tool_result = self.service.call_tool("describe_module", {"module_path": "app.py", "project_path": str(repo)})
        self.assertEqual(tool_result["entity_key"], module["entity_key"])

    def test_describe_module_passes_gateway_contract_to_llm_generation(self) -> None:
        repo = self.temp_dir / "semantic-gateway-repo"
        repo.mkdir()
        (repo / "app.py").write_text(
            """
\"\"\"Architecture helpers.\"\"\"

def build_packet(name: str) -> str:
    return name.upper()
""".strip()
            + "\n",
            encoding="utf-8",
        )

        self.config.output_compression.enabled = True
        self.config.output_compression.mode = "gateway_enforced"
        self.service = ObsmcpService(self.config)
        self.service.scan_codebase(project_path=str(repo), force_refresh=True)
        task = self.service.create_task(
            title="Architecture review for semantic output",
            description="Exercise gateway-enforced semantic generation.",
            tags=["architecture"],
            actor="test",
            project_path=str(repo),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(repo))

        captured: dict[str, Any] = {}

        def fake_generate_llm_description(
            entity: dict[str, Any],
            snippet: str,
            context: str | None = None,
            response_contract: str | None = None,
        ) -> dict[str, Any] | None:
            captured["response_contract"] = response_contract
            return {
                "purpose": "Summarizes the module.",
                "why_it_exists": "Exists to support architecture lookups.",
                "how_it_is_used": "Used through semantic description tools.",
                "inputs_outputs": "Takes source and returns description fields.",
                "side_effects": "No important side effects.",
                "risks": "Low risk.",
                "language": "Python",
                "llm_model": "fake-model",
                "llm_latency_ms": 1.2,
                "llm_input_tokens": 12,
                "llm_output_tokens": 24,
                "llm_generated": True,
            }

        with patch("server.semantic.generate_llm_description", side_effect=fake_generate_llm_description):
            result = self.service.refresh_semantic_description(module_path="app.py", project_path=str(repo), force_llm=True)

        self.assertEqual(result["entity_type"], "module")
        self.assertIn("## Enforced Response Contract", captured["response_contract"] or "")
        stats = self.service.get_token_usage_stats(project_path=str(repo), operation="generate_semantic_description")
        self.assertGreaterEqual(stats["event_count"], 1)

    def test_checkpoint_logging_syncs_into_obsidian(self) -> None:
        repo = self.temp_dir / "checkpoint-repo"
        repo.mkdir()
        source_file = repo / "app.py"
        source_file.write_text("def run() -> str:\n    return 'ok'\n", encoding="utf-8")

        task = self.service.create_task(
            title="Checkpoint task",
            description="Track subtask completion.",
            relevant_files=[str(source_file)],
            actor="test",
            project_path=str(repo),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(repo))

        checkpoint = self.service.log_checkpoint(
            task_id=task["id"],
            checkpoint_id="P2-03",
            title="finance_handler institution scoping",
            message="Completed institution scoping for finance_handler.",
            files=[str(source_file)],
            actor="test",
            project_path=str(repo),
        )
        self.assertEqual(checkpoint["checkpoint_id"], "P2-03")

        progress = self.service.get_task_progress(task["id"], project_path=str(repo))
        self.assertEqual(progress["completed_count"], 1)
        self.assertEqual(progress["recent_checkpoints"][0]["checkpoint_id"], "P2-03")

        snapshot = self.service.get_project_status_snapshot(project_path=str(repo))
        self.assertEqual(snapshot["current_task_progress"]["completed_count"], 1)
        self.assertEqual(snapshot["recent_checkpoints"][0]["checkpoint_id"], "P2-03")

        workspace = self.service.get_project_workspace_paths(project_path=str(repo))
        vault_dir = Path(workspace["vault_path"])
        current_task_note = (vault_dir / "Projects" / "Current Task.md").read_text(encoding="utf-8")
        status_snapshot_note = (vault_dir / "Projects" / "Status Snapshot.md").read_text(encoding="utf-8")
        self.assertIn("P2-03", current_task_note)
        self.assertIn("finance_handler institution scoping", current_task_note)
        self.assertIn("Recent Checkpoints", status_snapshot_note)

    def test_scan_codebase_prewarms_module_summaries(self) -> None:
        repo = self.temp_dir / "prewarm-repo"
        repo.mkdir()
        (repo / "app.py").write_text(
            "def run_task(name: str) -> str:\n    return f'run:{name}'\n",
            encoding="utf-8",
        )

        result = self.service.scan_codebase(project_path=str(repo), force_refresh=True)
        self.assertEqual(result["status"], "generated")
        self.assertTrue(result["semantic_prewarm"]["queued"])

        store = self.service._store(str(repo))
        deadline = time.time() + 5
        modules: list[dict[str, Any]] = []
        while time.time() < deadline:
            modules = store.get_cached_semantic_descriptions(entity_type="module", fresh_only=True, limit=10)
            if modules:
                break
            time.sleep(0.2)

        self.assertTrue(modules)
        self.assertTrue(any(item["file_path"] == "app.py" for item in modules))

        workspace = self.service.get_project_workspace_paths(project_path=str(repo))
        module_summaries_path = Path(workspace["vault_path"]) / "Research" / "Module Summaries.md"
        module_summaries = ""
        while time.time() < deadline:
            if module_summaries_path.exists():
                module_summaries = module_summaries_path.read_text(encoding="utf-8")
                if "app.py" in module_summaries:
                    break
            time.sleep(0.2)
        self.assertIn("app.py", module_summaries)
        self.assertNotIn("No module summaries cached yet.", module_summaries)

    def test_set_current_task_prewarms_semantics_from_relevant_files(self) -> None:
        repo = self.temp_dir / "set-current-prewarm-repo"
        repo.mkdir()
        source_file = repo / "feature.py"
        source_file.write_text("def run_feature() -> str:\n    return 'ready'\n", encoding="utf-8")

        self.config.semantic_auto_generate.on_create_task = False
        self.config.semantic_auto_generate.on_set_current_task = True

        task = self.service.create_task(
            title="Warm on set current task",
            description="Prime semantics from relevant files.",
            relevant_files=[str(source_file)],
            actor="test",
            project_path=str(repo),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(repo))

        store = self.service._store(str(repo))
        deadline = time.time() + 5
        modules: list[dict[str, Any]] = []
        while time.time() < deadline:
            modules = store.get_cached_semantic_descriptions(entity_type="module", fresh_only=True, limit=10)
            if any(item["file_path"] == "feature.py" for item in modules):
                break
            time.sleep(0.2)

        self.assertTrue(any(item["file_path"] == "feature.py" for item in modules))

    def test_startup_and_handoff_warm_semantics_for_current_task(self) -> None:
        repo = self.temp_dir / "startup-handoff-repo"
        repo.mkdir()
        source_file = repo / "app.py"
        source_file.write_text("def boot() -> str:\n    return 'ok'\n", encoding="utf-8")

        self.config.semantic_auto_generate.on_create_task = False
        self.config.semantic_auto_generate.on_set_current_task = False
        self.config.semantic_auto_generate.on_handoff = True
        self.config.semantic_auto_generate.on_startup = True
        self.config.semantic_auto_generate.wait_ms_on_handoff = 500
        self.config.semantic_auto_generate.wait_ms_on_startup = 500

        task = self.service.create_task(
            title="Warm on startup and handoff",
            description="Ensure startup and handoff prewarm relevant modules.",
            relevant_files=[str(source_file)],
            actor="test",
            project_path=str(repo),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(repo))

        startup = self.service.generate_startup_context(task_id=task["id"], project_path=str(repo))
        self.assertIn("Startup Context", startup["markdown"])

        store = self.service._store(str(repo))
        deadline = time.time() + 5
        modules: list[dict[str, Any]] = []
        while time.time() < deadline:
            modules = store.get_cached_semantic_descriptions(entity_type="module", fresh_only=True, limit=10)
            if any(item["file_path"] == "app.py" for item in modules):
                break
            time.sleep(0.2)
        self.assertTrue(any(item["file_path"] == "app.py" for item in modules))

        store.invalidate_semantic_cache(file_paths=["app.py"])
        handoff = self.service.create_handoff(
            summary="Warm semantics before handoff.",
            next_steps="Continue with the warmed module context.",
            task_id=task["id"],
            from_actor="test",
            to_actor="next-model",
            project_path=str(repo),
        )
        self.assertTrue(handoff["summary"].startswith("Warm semantics"))

        module_row = store.get_module_index("app.py")
        self.assertIsNotNone(module_row)
        deadline = time.time() + 5
        while time.time() < deadline:
            refreshed = store.get_semantic_description(module_row["entity_key"])
            if refreshed and not refreshed.get("stale"):
                break
            time.sleep(0.2)
        refreshed = store.get_semantic_description(module_row["entity_key"])
        self.assertIsNotNone(refreshed)
        self.assertFalse(refreshed.get("stale"))

    def test_checkpoint_auto_rollup_tracks_expected_phase_progress(self) -> None:
        repo = self.temp_dir / "checkpoint-rollup-repo"
        repo.mkdir()
        source_file = repo / "module.py"
        source_file.write_text("def run() -> str:\n    return 'done'\n", encoding="utf-8")

        self.config.checkpoints.auto_rollup = True
        self.config.checkpoints.auto_close_task = True

        task = self.service.create_task(
            title="Phase rollup task",
            description="Implement:\n- P9-01 add checkpoint logging\n- P9-02 add rollup rendering",
            relevant_files=[str(source_file)],
            actor="test",
            project_path=str(repo),
        )
        self.service.set_current_task(task_id=task["id"], actor="test", project_path=str(repo))

        self.service.log_checkpoint(
            task_id=task["id"],
            checkpoint_id="P9-01",
            title="add checkpoint logging",
            actor="test",
            project_path=str(repo),
        )
        first_progress = self.service.get_task_progress(task["id"], project_path=str(repo))
        self.assertEqual(first_progress["completed_count"], 1)
        self.assertEqual(first_progress["total_count"], 2)
        self.assertEqual(first_progress["remaining_checkpoints"], ["P9-02"])
        self.assertEqual(first_progress["phase_rollups"][0]["phase_key"], "P9")

        second = self.service.log_checkpoint(
            task_id=task["id"],
            checkpoint_id="P9-02",
            title="add rollup rendering",
            actor="test",
            project_path=str(repo),
        )
        self.assertIn("auto_closed_task", second)
        self.assertEqual(second["auto_closed_task"]["status"], "done")

        final_progress = self.service.get_task_progress(task["id"], project_path=str(repo))
        self.assertTrue(final_progress["all_expected_complete"])
        self.assertEqual(final_progress["phase_rollups"][0]["completed_count"], 2)
        self.assertEqual(final_progress["phase_rollups"][0]["total_count"], 2)

        workspace = self.service.get_project_workspace_paths(project_path=str(repo))
        current_task_note = (Path(workspace["vault_path"]) / "Projects" / "Current Task.md").read_text(encoding="utf-8")
        self.assertIn("P9: 2/2 complete", current_task_note)


if __name__ == "__main__":
    unittest.main()
