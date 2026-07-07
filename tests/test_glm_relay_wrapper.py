import argparse
import base64
import contextlib
import io
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "work" / "glm-relay"


def load_wrapper():
    loader = importlib.machinery.SourceFileLoader("glm_relay_wrapper", str(WRAPPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class GlmRelayWrapperTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_wrapper()

    def decode_jwt_payload(self, token):
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))

    def git_snapshot(self, available=True, status=None, reason=""):
        return {
            "available": available,
            "reason": reason,
            "repo_root": "/tmp/repo" if available else "",
            "head": "abc123" if available else "",
            "status": status or [],
        }

    def write_worker_run(
        self,
        root,
        name,
        metadata=None,
        stdout="",
        stderr="",
        exit_code="0\n",
        summary="summary\n",
    ):
        run_dir = Path(root) / name
        run_dir.mkdir(parents=True)
        if metadata is not None:
            (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (run_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
        (run_dir / "exit_code.txt").write_text(exit_code, encoding="utf-8")
        (run_dir / "summary.txt").write_text(summary, encoding="utf-8")
        return run_dir

    def test_generate_zai_jwt_from_raw_key(self):
        token, exp = self.mod.generate_zai_jwt("test-api-id.test-secret", ttl_seconds=60)

        self.assertEqual(len(token.split(".")), 3)
        self.assertGreater(exp, 0)
        payload = self.decode_jwt_payload(token)
        self.assertEqual(payload["api_key"], "test-api-id")
        self.assertIn("timestamp", payload)
        self.assertIn("exp", payload)
        self.assertNotIn("test-secret", token)

    def test_generate_zai_jwt_rejects_malformed_keys(self):
        for raw_key in ["", "missing-dot", ".secret", "api-id."]:
            with self.subTest(raw_key=raw_key):
                with self.assertRaises(SystemExit):
                    self.mod.generate_zai_jwt(raw_key)

    def test_write_profile_uses_responses_wire_api_and_model_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "glm52-relay.config.toml"
            args = argparse.Namespace(output=str(output), port=4459, model="glm-5.2")

            with contextlib.redirect_stdout(io.StringIO()):
                self.mod.write_profile(args)

            profile = output.read_text(encoding="utf-8")
            self.assertIn('model = "glm-5.2"', profile)
            self.assertIn('model_provider = "zai-relay"', profile)
            self.assertIn('base_url = "http://127.0.0.1:4459/v1"', profile)
            self.assertIn('wire_api = "responses"', profile)
            self.assertIn("work/glm-model-catalog.json", profile)
            self.assertIn("supports_parallel_tool_calls = true", profile)

    def test_default_tool_denylist_includes_subagent_variants(self):
        denylist = set(self.mod.DEFAULT_TOOL_DENYLIST.split(","))

        for name in [
            "spawn_agent",
            "wait_agent",
            "close_agent",
            "multi_agent_v1-spawn_agent",
            "multi_agent_v1-wait_agent",
            "multi_agent_v1-close_agent",
            "multi_agent_v1-resume_agent",
            "multi_agent_v1-send_input",
        ]:
            self.assertIn(name, denylist)

    def test_start_relay_writes_non_secret_state(self):
        captured = {}

        class FakePopen:
            pid = 4242

            def __init__(self, cmd, cwd, env, stdout, stderr, start_new_session):
                captured["cmd"] = cmd
                captured["cwd"] = cwd
                captured["env"] = env
                stdout.close()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.mod.STATE = tmp_path / "state"
            self.mod.OUT = tmp_path / "outputs"
            self.mod.PID_FILE = self.mod.STATE / "relay.pid"
            self.mod.STATE_FILE = self.mod.STATE / "state.json"
            self.mod.LOG_FILE = self.mod.OUT / "glm-relay.log"

            args = argparse.Namespace(
                restart=False,
                upstream="https://api.z.ai/api/coding/paas/v4",
                port=4453,
                model="glm-5.2",
                jwt_ttl_seconds=3600,
                refresh_margin_seconds=300,
                tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
                history_store="disk",
                model_map="",
                upstream_extra_params="",
                drop_upstream_params="",
                log_level="codex_relay=debug",
            )

            with mock.patch.dict(os.environ, {"ZAI_RAW_KEY": "test-api-id.test-secret", "CODEX_RELAY_BIN": "/bin/echo"}):
                with mock.patch.object(self.mod.subprocess, "Popen", FakePopen):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.mod.start_relay(args)

            state = json.loads(self.mod.STATE_FILE.read_text(encoding="utf-8"))
            state_text = json.dumps(state)
            self.assertEqual(state["pid"], 4242)
            self.assertEqual(state["model"], "glm-5.2")
            self.assertEqual(state["model_map"], "")
            self.assertEqual(state["upstream_extra_params"], "")
            self.assertEqual(state["drop_upstream_params"], "")
            self.assertIn("jwt_expires_at", state)
            self.assertNotIn("test-api-id.test-secret", state_text)
            self.assertNotIn("test-secret", state_text)
            self.assertNotIn(captured["env"]["CODEX_RELAY_API_KEY"], state_text)
            self.assertNotIn(captured["env"]["CODEX_RELAY_API_KEY"], self.mod.LOG_FILE.read_text(encoding="utf-8"))
            self.assertNotIn("ZAI_RAW_KEY", captured["env"])

    def test_relay_state_mismatches_detects_runtime_safety_changes(self):
        args = argparse.Namespace(
            upstream="https://api.z.ai/api/coding/paas/v4",
            port=4453,
            model="glm-5.2",
            tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
            history_store="disk",
            model_map="",
            upstream_extra_params="",
            drop_upstream_params="",
        )
        matching_state = {
            "upstream": args.upstream,
            "port": args.port,
            "model": args.model,
            "tool_denylist": args.tool_denylist,
            "history_store": args.history_store,
            "model_map": "",
            "upstream_extra_params": "",
            "drop_upstream_params": "",
        }

        self.assertEqual(self.mod.relay_state_mismatches(matching_state, args), [])

        unsafe_state = dict(matching_state)
        unsafe_state["tool_denylist"] = ""

        self.assertEqual(self.mod.relay_state_mismatches(unsafe_state, args), ["tool_denylist"])

    def test_restart_if_needed_restarts_when_saved_settings_mismatch(self):
        calls = []
        args = argparse.Namespace(
            upstream="https://api.z.ai/api/coding/paas/v4",
            port=4453,
            model="glm-5.2",
            refresh_margin_seconds=300,
            tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
            history_store="disk",
            model_map="",
            upstream_extra_params="",
            drop_upstream_params="",
        )
        state = {
            "upstream": args.upstream,
            "port": args.port,
            "model": args.model,
            "jwt_expires_at": 9999999999,
            "jwt_refresh_margin_seconds": args.refresh_margin_seconds,
            "tool_denylist": "",
            "history_store": args.history_store,
            "model_map": "",
            "upstream_extra_params": "",
            "drop_upstream_params": "",
        }

        with mock.patch.object(self.mod, "load_state", return_value=state):
            with mock.patch.object(self.mod, "relay_is_running", return_value=4242):
                with mock.patch.object(self.mod, "stop_relay", lambda: calls.append("stop")):
                    with mock.patch.object(self.mod, "start_relay", lambda _args: calls.append("start")):
                        with contextlib.redirect_stdout(io.StringIO()) as stdout:
                            self.mod.restart_if_needed(args)

        self.assertEqual(calls, ["stop", "start"])
        self.assertIn("relay settings changed", stdout.getvalue())

    def test_install_relay_auto_prefers_vendored_when_rust_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work = tmp_path / "work"
            venv_python = work / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
            vendor = tmp_path / "codex-relay"
            vendor.mkdir()
            (vendor / "pyproject.toml").write_text("[project]\nname = 'codex-relay'\n", encoding="utf-8")

            self.mod.WORK = work
            self.mod.STATE = work / ".glm-relay"
            self.mod.OUT = tmp_path / "outputs"
            self.mod.VENDORED_RELAY = vendor
            calls = []

            def fake_check_call(cmd):
                calls.append([str(part) for part in cmd])

            def fake_which(name):
                return f"/usr/bin/{name}" if name in {"cargo", "rustc"} else None

            with mock.patch.object(self.mod.subprocess, "check_call", fake_check_call):
                with mock.patch.object(self.mod.shutil, "which", fake_which):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.mod.install_relay(argparse.Namespace(python="", source="auto"))

            self.assertIn(str(vendor), calls[-1])

    def test_install_relay_auto_uses_pypi_without_rust(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work = tmp_path / "work"
            venv_python = work / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
            vendor = tmp_path / "codex-relay"
            vendor.mkdir()
            (vendor / "pyproject.toml").write_text("[project]\nname = 'codex-relay'\n", encoding="utf-8")

            self.mod.WORK = work
            self.mod.STATE = work / ".glm-relay"
            self.mod.OUT = tmp_path / "outputs"
            self.mod.VENDORED_RELAY = vendor
            calls = []

            with mock.patch.object(self.mod.subprocess, "check_call", lambda cmd: calls.append([str(part) for part in cmd])):
                with mock.patch.object(self.mod.shutil, "which", lambda _name: None):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.mod.install_relay(argparse.Namespace(python="", source="auto"))

            self.assertEqual(calls[-1][-1], "codex-relay")

    def test_live_smoke_requires_key_when_relay_is_not_running(self):
        args = argparse.Namespace(
            port=4453,
            upstream="https://api.z.ai/api/coding/paas/v4",
            model="glm-5.2",
            jwt_ttl_seconds=3600,
            refresh_margin_seconds=300,
            tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
            history_store="disk",
            model_map="",
            upstream_extra_params="",
            drop_upstream_params="",
            log_level="codex_relay=debug",
            timeout=1.0,
            include_tool_call=False,
            keep_running=False,
        )

        with mock.patch.object(self.mod, "relay_is_running", return_value=None):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit) as caught:
                    self.mod.live_smoke(args)

        self.assertIn("ZAI_RAW_KEY is required", str(caught.exception))

    def test_smoke_payloads_are_small_and_non_streaming(self):
        text_payload = self.mod.text_smoke_payload("glm-5.2")
        tool_payload = self.mod.tool_smoke_payload("glm-5.2")

        self.assertEqual(text_payload["model"], "glm-5.2")
        self.assertIs(text_payload["stream"], False)
        self.assertLessEqual(text_payload["max_output_tokens"], 512)

        tool_names = [tool["name"] for tool in tool_payload["tools"]]
        self.assertIn("exec_command", tool_names)
        self.assertIn("spawn_agent", tool_names)
        self.assertIs(tool_payload["stream"], False)

    def test_extract_smoke_outputs(self):
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "GLM_"},
                        {"type": "output_text", "text": "SMOKE_OK"},
                    ],
                },
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_1",
                    "arguments": "{\"cmd\":\"echo GLM_TOOL_OK\"}",
                },
            ]
        }

        self.assertEqual(self.mod.extract_output_text(response), "GLM_SMOKE_OK")
        self.assertEqual(
            self.mod.output_function_calls(response)[0]["name"],
            "exec_command",
        )

    def test_wait_for_local_port_times_out_cleanly(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        with self.assertRaises(SystemExit) as caught:
            self.mod.wait_for_local_port(port, timeout=0.01)

        self.assertIn("relay did not open port", str(caught.exception))

    def test_safe_slug_and_unique_worker_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.mod.OUT = tmp_path / "outputs"
            self.mod.WORKER_RUNS = self.mod.OUT / "glm-worker-runs"
            self.mod.STATE = tmp_path / "state"

            with mock.patch.object(self.mod.time, "strftime", return_value="20260707-010203"):
                first = self.mod.unique_worker_run_dir("Build worker lane!", "")
                second = self.mod.unique_worker_run_dir("Build worker lane!", "")
                fallback = self.mod.unique_worker_run_dir("!!!", "")

        self.assertEqual(first.name, "20260707-010203-build-worker-lane")
        self.assertEqual(second.name, "20260707-010203-build-worker-lane-2")
        self.assertEqual(fallback.name, "20260707-010203-worker")

    def test_find_codex_binary_uses_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex = Path(tmp) / "codex"
            codex.write_text("", encoding="utf-8")

            with mock.patch.dict(os.environ, {"CODEX_BIN": str(codex)}):
                self.assertEqual(self.mod.find_codex_binary(), str(codex))

    def test_find_codex_binary_fails_before_worker_run(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(self.mod.shutil, "which", return_value=None):
                with self.assertRaises(SystemExit) as caught:
                    self.mod.find_codex_binary()

        self.assertIn("codex CLI not found", str(caught.exception))

    def test_worker_preflight_refreshes_relay_and_writes_profile(self):
        calls = []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile = tmp_path / "glm52-relay.config.toml"
            args = argparse.Namespace(
                cwd=str(tmp_path),
                port=4453,
                timeout=5.0,
                codex_home=str(tmp_path),
                model="glm-5.2",
                codex_profile="glm52-relay",
                tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
                upstream="https://api.z.ai/api/coding/paas/v4",
                jwt_ttl_seconds=3600,
                refresh_margin_seconds=300,
                history_store="disk",
                model_map="",
                upstream_extra_params="",
                drop_upstream_params="",
                log_level="codex_relay=debug",
            )

            with mock.patch.object(self.mod, "find_codex_binary", return_value="/bin/codex"):
                with mock.patch.object(self.mod, "restart_if_needed", lambda _args: calls.append("refresh")):
                    with mock.patch.object(self.mod, "wait_for_local_port", lambda port, timeout: calls.append(("wait", port, timeout))):
                        with contextlib.redirect_stdout(io.StringIO()):
                            codex_bin, worker_cwd, codex_home = self.mod.worker_preflight(args)

            self.assertEqual(codex_bin, "/bin/codex")
            self.assertEqual(worker_cwd, tmp_path.resolve())
            self.assertEqual(codex_home, tmp_path.resolve())
            self.assertEqual(calls[0], "refresh")
            self.assertEqual(calls[1][0], "wait")
            self.assertIn('wire_api = "responses"', profile.read_text(encoding="utf-8"))

    def test_worker_preflight_rejects_missing_cwd(self):
        args = argparse.Namespace(
            cwd="/definitely/missing/glm-worker-cwd",
            port=4453,
            timeout=5.0,
            codex_home="/tmp",
            model="glm-5.2",
            codex_profile="glm52-relay",
        )

        with mock.patch.object(self.mod, "find_codex_binary", return_value="/bin/codex"):
            with self.assertRaises(SystemExit) as caught:
                self.mod.worker_preflight(args)

        self.assertIn("worker cwd is not a directory", str(caught.exception))

    def test_run_worker_captures_success_artifacts_with_redacted_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.mod.OUT = tmp_path / "outputs"
            self.mod.WORKER_RUNS = self.mod.OUT / "glm-worker-runs"
            self.mod.STATE = tmp_path / "state"
            self.mod.STATE_FILE = self.mod.STATE / "state.json"
            self.mod.LOG_FILE = self.mod.OUT / "glm-relay.log"
            self.mod.save_state({"port": 4453, "pid": 1234, "log_file": str(self.mod.LOG_FILE)})
            task = "Implement the tiny worker fixture"
            args = argparse.Namespace(
                task=task,
                label="unit test",
                cwd=str(tmp_path),
                timeout=30.0,
                codex_profile="glm52-relay",
                codex_home=str(tmp_path / "codex-home"),
                model="glm-5.2",
                port=4453,
                tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
            )
            captured = {}

            def fake_run(argv, cwd, env, text, capture_output, timeout):
                captured["argv"] = argv
                captured["cwd"] = cwd
                captured["env"] = env
                return subprocess.CompletedProcess(argv, 0, "ok\n", "")

            env_patch = {
                "ZAI_RAW_KEY": "test-api-id.test-secret",
                "ZAI_API_KEY": "another-zai-secret",
                "CODEX_RELAY_API_KEY": "relay-token",
                "CODEX_RELAY_UPSTREAM": "https://example.invalid",
            }
            with mock.patch.dict(os.environ, env_patch):
                with mock.patch.object(self.mod, "worker_preflight", return_value=("/bin/codex", tmp_path, tmp_path / "codex-home")):
                    with mock.patch.object(self.mod, "git_context", return_value=self.git_snapshot()):
                        with mock.patch.object(self.mod.subprocess, "run", fake_run):
                            with mock.patch.object(self.mod, "relay_is_running", return_value=1234):
                                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                                    code = self.mod.run_worker(args)

            run_dirs = list((self.mod.WORKER_RUNS).iterdir())
            self.assertEqual(code, 0)
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual((run_dir / "prompt.txt").read_text(encoding="utf-8"), task)
            self.assertEqual((run_dir / "stdout.txt").read_text(encoding="utf-8"), "ok\n")
            self.assertEqual(metadata["pid"], 1234)
            self.assertEqual(metadata["argv"][-1], "<redacted-task-prompt>")
            self.assertIn("git", metadata)
            self.assertIn("before", metadata["git"])
            self.assertIn("after", metadata["git"])
            self.assertEqual(metadata["git"]["changed_files"], [])
            self.assertNotIn(task, json.dumps(metadata))
            self.assertEqual(captured["argv"][-1], task)
            self.assertEqual(captured["env"]["CODEX_HOME"], str(tmp_path / "codex-home"))
            self.assertNotIn("ZAI_RAW_KEY", captured["env"])
            self.assertNotIn("ZAI_API_KEY", captured["env"])
            self.assertNotIn("CODEX_RELAY_API_KEY", captured["env"])
            self.assertNotIn("CODEX_RELAY_UPSTREAM", captured["env"])
            self.assertEqual(metadata["profile_output"], str(tmp_path / "codex-home" / "glm52-relay.config.toml"))
            self.assertIn("worker passed", stdout.getvalue())

    def test_run_worker_metadata_ignores_stale_state_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.mod.OUT = tmp_path / "outputs"
            self.mod.WORKER_RUNS = self.mod.OUT / "glm-worker-runs"
            self.mod.STATE = tmp_path / "state"
            self.mod.STATE_FILE = self.mod.STATE / "state.json"
            self.mod.save_state({"port": 4453, "pid": 9999})
            args = argparse.Namespace(
                task="Inspect stale pid handling",
                label="",
                cwd=str(tmp_path),
                timeout=30.0,
                codex_profile="glm52-relay",
                codex_home=str(tmp_path / "codex-home"),
                model="glm-5.2",
                port=4453,
                tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
            )

            with mock.patch.object(self.mod, "worker_preflight", return_value=("/bin/codex", tmp_path, tmp_path / "codex-home")):
                with mock.patch.object(self.mod, "git_context", return_value=self.git_snapshot()):
                    with mock.patch.object(
                        self.mod.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(["codex"], 0, "", ""),
                    ):
                        with mock.patch.object(self.mod, "relay_is_running", return_value=None):
                            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                                code = self.mod.run_worker(args)

            run_dirs = list((self.mod.WORKER_RUNS).iterdir())
            self.assertEqual(code, 0)
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertIsNone(metadata["pid"])
            self.assertNotEqual(metadata["pid"], 9999)
            self.assertIn("worker passed", stdout.getvalue())

    def test_run_worker_captures_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.mod.OUT = tmp_path / "outputs"
            self.mod.WORKER_RUNS = self.mod.OUT / "glm-worker-runs"
            self.mod.STATE = tmp_path / "state"
            self.mod.STATE_FILE = self.mod.STATE / "state.json"
            self.mod.LOG_FILE = self.mod.OUT / "glm-relay.log"
            self.mod.save_state({"port": 4453})
            args = argparse.Namespace(
                task="Fail usefully",
                label="",
                cwd=str(tmp_path),
                timeout=30.0,
                codex_profile="glm52-relay",
                codex_home=str(tmp_path / "codex-home"),
                model="glm-5.2",
                port=4453,
                tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
            )

            with mock.patch.object(self.mod, "worker_preflight", return_value=("/bin/codex", tmp_path, tmp_path / "codex-home")):
                with mock.patch.object(self.mod, "git_context", return_value=self.git_snapshot()):
                    with mock.patch.object(
                        self.mod.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(["codex"], 7, "", "bad\n"),
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            code = self.mod.run_worker(args)

            run_dir = next(self.mod.WORKER_RUNS.iterdir())
            self.assertEqual(code, 7)
            self.assertEqual((run_dir / "exit_code.txt").read_text(encoding="utf-8"), "7\n")
            self.assertEqual((run_dir / "stderr.txt").read_text(encoding="utf-8"), "bad\n")

    def test_run_worker_captures_preflight_failure_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.mod.OUT = tmp_path / "outputs"
            self.mod.WORKER_RUNS = self.mod.OUT / "glm-worker-runs"
            self.mod.STATE = tmp_path / "state"
            self.mod.STATE_FILE = self.mod.STATE / "state.json"
            self.mod.save_state({})
            args = argparse.Namespace(
                task="Do not reach Codex",
                label="preflight",
                cwd=str(tmp_path),
                timeout=30.0,
                codex_profile="glm52-relay",
                codex_home=str(tmp_path / "codex-home"),
                model="glm-5.2",
                port=4453,
                tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
            )

            with mock.patch.object(self.mod, "worker_preflight", side_effect=SystemExit("codex CLI not found")):
                with mock.patch.object(self.mod, "git_context", return_value=self.git_snapshot()):
                    with mock.patch.object(self.mod.subprocess, "run") as run:
                        with contextlib.redirect_stdout(io.StringIO()):
                            code = self.mod.run_worker(args)

            run.assert_not_called()
            run_dir = next(self.mod.WORKER_RUNS.iterdir())
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 1)
            self.assertEqual(metadata["error"], "codex CLI not found")
            self.assertIn("codex CLI not found", (run_dir / "stderr.txt").read_text(encoding="utf-8"))
            self.assertEqual((run_dir / "prompt.txt").read_text(encoding="utf-8"), "Do not reach Codex")

    def test_run_worker_captures_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.mod.OUT = tmp_path / "outputs"
            self.mod.WORKER_RUNS = self.mod.OUT / "glm-worker-runs"
            self.mod.STATE = tmp_path / "state"
            self.mod.STATE_FILE = self.mod.STATE / "state.json"
            self.mod.save_state({})
            args = argparse.Namespace(
                task="Timeout",
                label="",
                cwd=str(tmp_path),
                timeout=1.0,
                codex_profile="glm52-relay",
                codex_home=str(tmp_path / "codex-home"),
                model="glm-5.2",
                port=4453,
                tool_denylist=self.mod.DEFAULT_TOOL_DENYLIST,
            )

            def raise_timeout(*_args, **_kwargs):
                raise subprocess.TimeoutExpired(cmd=["codex"], timeout=1.0, output=b"partial\n", stderr=b"slow\n")

            with mock.patch.object(self.mod, "worker_preflight", return_value=("/bin/codex", tmp_path, tmp_path / "codex-home")):
                with mock.patch.object(self.mod, "git_context", return_value=self.git_snapshot()):
                    with mock.patch.object(self.mod.subprocess, "run", raise_timeout):
                        with contextlib.redirect_stdout(io.StringIO()):
                            code = self.mod.run_worker(args)

            run_dir = next(self.mod.WORKER_RUNS.iterdir())
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 124)
            self.assertEqual(metadata["error"], "worker timed out after 1.0s")
            self.assertEqual((run_dir / "stdout.txt").read_text(encoding="utf-8"), "partial\n")
            self.assertEqual((run_dir / "stderr.txt").read_text(encoding="utf-8"), "slow\n")

    def test_git_context_captures_status_and_head(self):
        calls = []

        def fake_git(argv, text, capture_output, timeout):
            calls.append(argv)
            if argv[-1] == "--show-toplevel":
                return subprocess.CompletedProcess(argv, 0, "/tmp/repo\n", "")
            if argv[-1] == "HEAD":
                return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
            return subprocess.CompletedProcess(argv, 0, " M README.md\n?? notes.txt\n", "")

        with mock.patch.object(self.mod.subprocess, "run", fake_git):
            snapshot = self.mod.git_context(Path("/tmp"))

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["repo_root"], "/tmp/repo")
        self.assertEqual(snapshot["head"], "abc123")
        self.assertEqual(
            snapshot["status"],
            [
                {"status": "M", "path": "README.md"},
                {"status": "??", "path": "notes.txt"},
            ],
        )
        self.assertEqual(len(calls), 3)

    def test_git_context_fails_softly_outside_repo(self):
        def fake_git(argv, text, capture_output, timeout):
            return subprocess.CompletedProcess(argv, 128, "", "not a git repository")

        with mock.patch.object(self.mod.subprocess, "run", fake_git):
            snapshot = self.mod.git_context(Path("/tmp"))

        self.assertFalse(snapshot["available"])
        self.assertIn("not a git repository", snapshot["reason"])
        self.assertEqual(snapshot["status"], [])

    def test_git_status_delta_distinguishes_file_states(self):
        before = self.git_snapshot(status=[
            {"status": "M", "path": "preexisting.txt"},
            {"status": "A", "path": "cleaned.txt"},
            {"status": "M", "path": "changed.txt"},
        ])
        after = self.git_snapshot(status=[
            {"status": "M", "path": "preexisting.txt"},
            {"status": "MM", "path": "changed.txt"},
            {"status": "??", "path": "new.txt"},
        ])

        delta = self.mod.git_status_delta(before, after)

        self.assertEqual(
            delta,
            [
                {
                    "path": "changed.txt",
                    "before_status": "M",
                    "after_status": "MM",
                    "change": "changed",
                },
                {
                    "path": "cleaned.txt",
                    "before_status": "A",
                    "after_status": "",
                    "change": "cleaned",
                },
                {
                    "path": "new.txt",
                    "before_status": "",
                    "after_status": "??",
                    "change": "new",
                },
                {
                    "path": "preexisting.txt",
                    "before_status": "M",
                    "after_status": "M",
                    "change": "preexisting",
                },
            ],
        )

    def test_resolve_worker_run_latest_and_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.WORKER_RUNS = Path(tmp) / "runs"
            older = self.write_worker_run(self.mod.WORKER_RUNS, "20260707-000001-old")
            newer = self.write_worker_run(self.mod.WORKER_RUNS, "20260707-000002-new")
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))

            self.assertEqual(self.mod.resolve_worker_run("latest"), newer)
            self.assertEqual(self.mod.resolve_worker_run(older.name), older.resolve())
            self.assertEqual(self.mod.resolve_worker_run(str(newer)), newer.resolve())

    def test_resolve_worker_run_errors_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.WORKER_RUNS = Path(tmp) / "runs"

            with self.assertRaises(SystemExit) as caught:
                self.mod.resolve_worker_run("latest")

        self.assertIn("no worker runs found", str(caught.exception))

    def test_load_worker_run_tolerates_malformed_and_missing_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.WORKER_RUNS = Path(tmp) / "runs"
            run_dir = self.mod.WORKER_RUNS / "partial"
            run_dir.mkdir(parents=True)
            (run_dir / "metadata.json").write_text("{not json", encoding="utf-8")
            (run_dir / "stderr.txt").write_text("bad\n", encoding="utf-8")
            (run_dir / "exit_code.txt").write_text("7\n", encoding="utf-8")

            bundle = self.mod.load_worker_run("partial")
            review = self.mod.build_worker_review(bundle)

        self.assertEqual(review["exit_code"], 7)
        self.assertEqual(review["status"], "failed")
        self.assertIn("bad", review["stderr_tail"])
        self.assertTrue(any("malformed JSON" in warning for warning in review["warnings"]))
        self.assertTrue(any("missing artifact: stdout.txt" in warning for warning in review["warnings"]))

    def test_review_worker_human_output_is_read_only_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.WORKER_RUNS = Path(tmp) / "runs"
            metadata = {
                "model": "glm-5.2",
                "cwd": str(Path(tmp)),
                "elapsed_seconds": 1.25,
                "pid": 1234,
                "exit_code": 0,
                "argv": ["codex", "exec", "secret prompt"],
                "git": {
                    "before": self.git_snapshot(status=[]),
                    "after": self.git_snapshot(status=[{"status": "M", "path": "README.md"}]),
                    "changed_files": [
                        {
                            "path": "README.md",
                            "before_status": "",
                            "after_status": "M",
                            "change": "new",
                        }
                    ],
                },
            }
            self.write_worker_run(
                self.mod.WORKER_RUNS,
                "review-me",
                metadata=metadata,
                stdout="\n".join(f"out {i}" for i in range(6)),
            )
            args = argparse.Namespace(
                target="review-me",
                json_output=False,
                stdout_lines=2,
                stderr_lines=2,
            )

            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = self.mod.review_worker(args)

        text = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("worker review: passed exit_code=0", text)
        self.assertIn("README.md", text)
        self.assertIn("out 4", text)
        self.assertIn("out 5", text)
        self.assertNotIn("out 3", text)
        self.assertNotIn("secret prompt", text)
        self.assertIn("inspect the actual git diff", text)

    def test_review_worker_json_output_is_parseable_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.WORKER_RUNS = Path(tmp) / "runs"
            metadata = {
                "model": "glm-5.2",
                "cwd": str(Path(tmp)),
                "exit_code": 0,
                "argv": ["codex", "exec", "raw task text"],
                "git": {
                    "before": self.git_snapshot(status=[]),
                    "after": self.git_snapshot(status=[{"status": "M", "path": "work/glm-relay"}]),
                },
            }
            self.write_worker_run(
                self.mod.WORKER_RUNS,
                "json-me",
                metadata=metadata,
                stdout="ok\n",
            )
            args = argparse.Namespace(
                target="json-me",
                json_output=True,
                stdout_lines=5,
                stderr_lines=5,
            )

            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = self.mod.review_worker(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["metadata"]["argv"][-1], "<redacted-task-prompt>")
        self.assertNotIn("raw task text", stdout.getvalue())
        self.assertEqual(payload["changed_files"][0]["path"], "work/glm-relay")
        self.assertEqual(payload["stdout_tail"], ["ok"])

    def test_review_warns_when_empty_delta_came_from_unavailable_git_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.WORKER_RUNS = Path(tmp) / "runs"
            metadata = {
                "exit_code": 0,
                "git": {
                    "before": self.git_snapshot(available=False, reason="not a git repository"),
                    "after": self.git_snapshot(available=False, reason="not a git repository"),
                    "changed_files": [],
                },
            }
            self.write_worker_run(
                self.mod.WORKER_RUNS,
                "no-git",
                metadata=metadata,
            )

            bundle = self.mod.load_worker_run("no-git")
            review = self.mod.build_worker_review(bundle)

        self.assertEqual(review["changed_files"], [])
        self.assertTrue(any("git context unavailable" in warning for warning in review["warnings"]))

    def test_run_worker_parser_accepts_task_and_common_args(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        p = sub.add_parser("run-worker")
        self.mod.add_common_start_args(p)
        p.add_argument("task")
        p.add_argument("--label", default="")
        p.add_argument("--cwd", default=str(self.mod.ROOT))
        p.add_argument("--timeout", type=float, default=900.0)
        p.add_argument("--codex-profile", default=self.mod.DEFAULT_CODEX_PROFILE)
        p.add_argument("--codex-home", default=self.mod.DEFAULT_CODEX_HOME)
        p.set_defaults(func=self.mod.run_worker)

        args = parser.parse_args(["run-worker", "--port", "4459", "Do the work"])

        self.assertEqual(args.command, "run-worker")
        self.assertEqual(args.port, 4459)
        self.assertEqual(args.task, "Do the work")
        self.assertEqual(args.codex_profile, "glm52-relay")
        self.assertEqual(args.codex_home, "~/.codex")

    def test_review_worker_parser_accepts_defaults_target_and_json(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        p = sub.add_parser("review-worker")
        p.add_argument("target", nargs="?", default="latest")
        p.add_argument("--json", action="store_true", dest="json_output")
        p.add_argument("--stdout-lines", type=int, default=self.mod.DEFAULT_REVIEW_TAIL_LINES)
        p.add_argument("--stderr-lines", type=int, default=self.mod.DEFAULT_REVIEW_TAIL_LINES)
        p.set_defaults(func=self.mod.review_worker)

        default_args = parser.parse_args(["review-worker"])
        json_args = parser.parse_args(["review-worker", "run-name", "--json", "--stdout-lines", "3"])

        self.assertEqual(default_args.target, "latest")
        self.assertFalse(default_args.json_output)
        self.assertEqual(json_args.target, "run-name")
        self.assertTrue(json_args.json_output)
        self.assertEqual(json_args.stdout_lines, 3)


if __name__ == "__main__":
    unittest.main()
