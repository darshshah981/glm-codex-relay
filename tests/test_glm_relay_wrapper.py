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
                with mock.patch.object(self.mod.subprocess, "run", raise_timeout):
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = self.mod.run_worker(args)

            run_dir = next(self.mod.WORKER_RUNS.iterdir())
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 124)
            self.assertEqual(metadata["error"], "worker timed out after 1.0s")
            self.assertEqual((run_dir / "stdout.txt").read_text(encoding="utf-8"), "partial\n")
            self.assertEqual((run_dir / "stderr.txt").read_text(encoding="utf-8"), "slow\n")

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


if __name__ == "__main__":
    unittest.main()
