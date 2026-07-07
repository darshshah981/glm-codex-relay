//! GLM relay compatibility fixtures (offline).
//!
//! These fixtures are minimized, redacted representatives of the Codex
//! Responses shapes the GLM wrapper cares about. They lock the outbound Chat
//! Completions body so changes to translation behavior produce a readable diff.

use codex_relay::session::SessionStore;
use codex_relay::translate::to_chat_request;
use codex_relay::types::ResponsesRequest;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

static ENV_LOCK: Mutex<()> = Mutex::new(());

const FIXTURE_DIR: &str = "tests/fixtures/codex_glm_current";
const DEFAULT_TOOL_DENYLIST: &str = "spawn_agent,wait_agent,close_agent,\
multi_agent_v1-spawn_agent,multi_agent_v1-wait_agent,multi_agent_v1-close_agent,\
multi_agent_v1-resume_agent,multi_agent_v1-send_input";

fn fixture_path(name: &str) -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push(FIXTURE_DIR);
    p.push(name);
    p
}

fn response_fixture(name: &str) -> ResponsesRequest {
    let p = fixture_path(name);
    let bytes = std::fs::read(&p).unwrap_or_else(|e| panic!("read {}: {e}", p.display()));
    serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("parse {}: {e}", p.display()))
}

fn expected_chat(name: &str) -> Value {
    let p = fixture_path(&format!("expected_chat/{name}"));
    let bytes = std::fs::read(&p).unwrap_or_else(|e| panic!("read {}: {e}", p.display()));
    serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("parse {}: {e}", p.display()))
}

fn assert_golden_translation(name: &str) {
    let _guard = ENV_LOCK.lock().expect("env lock");
    std::env::set_var("CODEX_RELAY_TOOL_DENYLIST", DEFAULT_TOOL_DENYLIST);

    let req = response_fixture(name);
    let chat = to_chat_request(&req, Vec::new(), &SessionStore::new());
    let actual = serde_json::to_value(&chat).expect("serialize chat request");
    let expected = expected_chat(name);

    std::env::remove_var("CODEX_RELAY_TOOL_DENYLIST");
    assert_eq!(actual, expected, "golden translation mismatch for {name}");
}

#[test]
fn text_only_request_matches_expected_chat_body() {
    assert_golden_translation("text_only.json");
}

#[test]
fn reasoning_request_drops_replayed_reasoning_item_and_enables_glm_thinking() {
    assert_golden_translation("reasoning_request.json");
}

#[test]
fn tool_request_filters_denied_subagent_tools_and_flattens_namespace_tools() {
    assert_golden_translation("tool_call_request.json");
}

#[test]
fn tool_output_followup_maps_to_chat_tool_message() {
    assert_golden_translation("tool_output_followup.json");
}

#[test]
fn parallel_function_call_replay_groups_calls_in_one_assistant_message() {
    assert_golden_translation("parallel_tools_request.json");
}

#[test]
fn all_glm_response_fixtures_parse() {
    for name in [
        "text_only.json",
        "reasoning_request.json",
        "tool_call_request.json",
        "tool_output_followup.json",
        "parallel_tools_request.json",
    ] {
        let _ = response_fixture(name);
    }
}

#[test]
fn glm_fixtures_do_not_contain_common_secret_shapes() {
    fn scan_file(path: &Path) {
        let text = std::fs::read_to_string(path)
            .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        let forbidden = [
            "api_id.secret",
            "Authorization:",
            "Bearer ",
            "gho_",
            "/Users/",
            "openai.sk-",
        ];
        for needle in forbidden {
            assert!(
                !text.contains(needle),
                "fixture {} contains forbidden pattern {needle:?}",
                path.display()
            );
        }
    }

    fn walk(dir: &Path) {
        for entry in
            std::fs::read_dir(dir).unwrap_or_else(|e| panic!("read_dir {}: {e}", dir.display()))
        {
            let path = entry.expect("dir entry").path();
            if path.is_dir() {
                walk(&path);
            } else {
                scan_file(&path);
            }
        }
    }

    walk(&fixture_path(""));
}
