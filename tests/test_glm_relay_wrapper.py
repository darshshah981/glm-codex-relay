import argparse
import base64
import contextlib
import io
import importlib.machinery
import importlib.util
import json
import os
import socket
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
            self.assertIn("jwt_expires_at", state)
            self.assertNotIn("test-api-id.test-secret", state_text)
            self.assertNotIn("test-secret", state_text)
            self.assertNotIn(captured["env"]["CODEX_RELAY_API_KEY"], state_text)
            self.assertNotIn(captured["env"]["CODEX_RELAY_API_KEY"], self.mod.LOG_FILE.read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()
