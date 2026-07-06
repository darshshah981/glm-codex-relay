//! GLM stream compatibility tests (offline).
//!
//! These tests run the relay binary against a local fake Chat Completions
//! upstream so GLM-style SSE chunks can be replayed without live credentials.

use axum::{
    body::Body,
    extract::State,
    http::{header, StatusCode},
    response::Response,
    routing::{get, post},
    Router,
};
use eventsource_stream::Eventsource;
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const RELAY_BIN: &str = env!("CARGO_BIN_EXE_codex-relay");
const FIXTURE_DIR: &str = "tests/fixtures/codex_glm_current";
const DEFAULT_TOOL_DENYLIST: &str = "spawn_agent,wait_agent,close_agent,\
multi_agent_v1-spawn_agent,multi_agent_v1-wait_agent,multi_agent_v1-close_agent,\
multi_agent_v1-resume_agent,multi_agent_v1-send_input";

#[derive(Clone)]
struct MockState {
    bodies: Arc<Mutex<Vec<Value>>>,
    responses: Arc<Mutex<VecDeque<String>>>,
    status: StatusCode,
}

fn fixture_path(name: &str) -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push(FIXTURE_DIR);
    p.push(name);
    p
}

fn fixture_json(name: &str) -> Value {
    let p = fixture_path(name);
    let bytes = std::fs::read(&p).unwrap_or_else(|e| panic!("read {}: {e}", p.display()));
    serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("parse {}: {e}", p.display()))
}

fn fixture_text(name: &str) -> String {
    let p = fixture_path(name);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("read {}: {e}", p.display()))
}

async fn models_handler() -> axum::Json<Value> {
    axum::Json(json!({"data": [{"id": "glm-5.2"}]}))
}

async fn chat_handler(State(state): State<MockState>, req: axum::extract::Request) -> Response {
    let bytes = match axum::body::to_bytes(req.into_body(), 1_000_000).await {
        Ok(bytes) => bytes,
        Err(_) => {
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .body(Body::from("bad body"))
                .unwrap();
        }
    };
    let body: Value = serde_json::from_slice(&bytes).expect("chat request json");
    state.bodies.lock().unwrap().push(body);

    if !state.status.is_success() {
        return Response::builder()
            .status(state.status)
            .body(Body::from("upstream unavailable"))
            .unwrap();
    }

    let sse = state
        .responses
        .lock()
        .unwrap()
        .pop_front()
        .unwrap_or_else(|| fixture_text("glm_streams/text.sse"));

    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/event-stream")
        .body(Body::from(sse))
        .unwrap()
}

async fn spawn_mock_upstream_with(
    responses: Vec<String>,
    status: StatusCode,
) -> (u16, Arc<Mutex<Vec<Value>>>) {
    let bodies = Arc::new(Mutex::new(Vec::new()));
    let state = MockState {
        bodies: bodies.clone(),
        responses: Arc::new(Mutex::new(VecDeque::from(responses))),
        status,
    };
    let app = Router::new()
        .route("/v1/models", get(models_handler))
        .route("/v1/chat/completions", post(chat_handler))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind mock upstream");
    let port = listener.local_addr().expect("mock upstream addr").port();
    tokio::spawn(async move {
        axum::serve(listener, app)
            .await
            .expect("mock upstream serve");
    });
    (port, bodies)
}

async fn spawn_mock_upstream(responses: Vec<String>) -> (u16, Arc<Mutex<Vec<Value>>>) {
    spawn_mock_upstream_with(responses, StatusCode::OK).await
}

async fn post_stream_events(relay: &Relay, body: Value) -> Vec<(String, Value)> {
    let resp = reqwest::Client::new()
        .post(relay.url("/v1/responses"))
        .json(&body)
        .send()
        .await
        .expect("POST /v1/responses");
    assert!(resp.status().is_success(), "status {}", resp.status());

    let mut events = resp.bytes_stream().eventsource();
    let mut out = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(8);
    while let Some(ev) = tokio::time::timeout(deadline - Instant::now(), events.next())
        .await
        .expect("stream timeout")
    {
        let ev = ev.expect("sse parse");
        let event = ev.event;
        let data: Value = serde_json::from_str(&ev.data).expect("event json");
        let terminal = event == "response.completed" || event == "response.failed";
        out.push((event, data));
        if terminal {
            return out;
        }
    }

    panic!("terminal response event");
}

fn completed(events: &[(String, Value)]) -> &Value {
    events
        .iter()
        .find_map(|(event, data)| (event == "response.completed").then_some(data))
        .expect("response.completed")
}

fn failed(events: &[(String, Value)]) -> &Value {
    events
        .iter()
        .find_map(|(event, data)| (event == "response.failed").then_some(data))
        .expect("response.failed")
}

struct Relay {
    child: Child,
    port: u16,
}

impl Drop for Relay {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Relay {
    fn spawn(upstream: &str) -> Self {
        let mut command = Command::new(RELAY_BIN);
        command
            .env("CODEX_RELAY_PORT", "0")
            .env("CODEX_RELAY_UPSTREAM", upstream)
            .env("CODEX_RELAY_API_KEY", "")
            .env("CODEX_RELAY_TOOL_DENYLIST", DEFAULT_TOOL_DENYLIST)
            .env("RUST_LOG", "codex_relay=info")
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = command.spawn().expect("spawn codex-relay");
        let port = Self::read_listening_port(&mut child);
        let mut handle = Relay { child, port };
        handle.wait_ready();
        handle
    }

    fn read_listening_port(child: &mut Child) -> u16 {
        use std::io::{BufRead, BufReader};
        use std::sync::mpsc;
        let stdout = child.stdout.take().expect("relay stdout");
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            let mut tx = Some(tx);
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
                if let Some(sender) = tx.as_ref() {
                    if let Some(rest) = line.split("listening on 127.0.0.1:").nth(1) {
                        if let Some(port) = rest
                            .split(|c: char| !c.is_ascii_digit())
                            .next()
                            .and_then(|s| s.parse::<u16>().ok())
                        {
                            let _ = sender.send(port);
                            tx = None;
                        }
                    }
                }
            }
        });
        rx.recv_timeout(Duration::from_secs(8))
            .expect("relay did not report a listening port")
    }

    fn wait_ready(&mut self) {
        use std::net::{SocketAddr, TcpStream};
        let deadline = Instant::now() + Duration::from_secs(8);
        let addr = SocketAddr::from(([127, 0, 0, 1], self.port));
        while Instant::now() < deadline {
            if TcpStream::connect_timeout(&addr, Duration::from_millis(100)).is_ok() {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        panic!("relay did not become ready on :{}", self.port);
    }

    fn url(&self, path: &str) -> String {
        format!("http://127.0.0.1:{}{}", self.port, path)
    }
}

#[tokio::test]
async fn text_stream_emits_completed_message_and_cached_usage() {
    let (upstream_port, _bodies) =
        spawn_mock_upstream(vec![fixture_text("glm_streams/text.sse")]).await;
    let relay = Relay::spawn(&format!("http://127.0.0.1:{upstream_port}/v1"));

    let events = post_stream_events(&relay, fixture_json("text_only.json")).await;
    let completed = completed(&events);

    assert!(events.iter().any(|(event, _)| event == "response.created"));
    assert!(events.iter().any(|(event, data)| {
        event == "response.output_item.added" && data["item"]["type"] == "message"
    }));
    assert!(events
        .iter()
        .any(|(event, data)| { event == "response.output_text.delta" && data["delta"] == "OK" }));
    assert_eq!(
        completed["response"]["output"][0]["content"],
        json!([{"type": "output_text", "text": "OK"}])
    );
    assert_eq!(
        completed["response"]["usage"],
        json!({
            "input_tokens": 7,
            "output_tokens": 2,
            "total_tokens": 9,
            "input_tokens_details": {"cached_tokens": 3}
        })
    );
}

#[tokio::test]
async fn reasoning_content_stream_emits_reasoning_events() {
    let (upstream_port, _bodies) =
        spawn_mock_upstream(vec![fixture_text("glm_streams/reasoning.sse")]).await;
    let relay = Relay::spawn(&format!("http://127.0.0.1:{upstream_port}/v1"));

    let events = post_stream_events(&relay, fixture_json("reasoning_request.json")).await;
    let reasoning_deltas: Vec<&Value> = events
        .iter()
        .filter_map(|(event, data)| {
            (event == "response.reasoning_summary_text.delta").then_some(data)
        })
        .collect();
    assert_eq!(reasoning_deltas.len(), 2);
    assert_eq!(reasoning_deltas[0]["delta"], "think ");
    assert_eq!(reasoning_deltas[1]["delta"], "briefly");

    let completed = completed(&events);
    assert_eq!(completed["response"]["output"][0]["type"], "reasoning");
    assert_eq!(
        completed["response"]["output"][0]["summary"],
        json!([{"type": "summary_text", "text": "think briefly"}])
    );
    assert_eq!(completed["response"]["output"][1]["type"], "message");
}

#[tokio::test]
async fn reasoning_alias_stream_emits_reasoning_events() {
    let (upstream_port, _bodies) =
        spawn_mock_upstream(vec![fixture_text("glm_streams/reasoning_alias.sse")]).await;
    let relay = Relay::spawn(&format!("http://127.0.0.1:{upstream_port}/v1"));

    let events = post_stream_events(&relay, fixture_json("reasoning_request.json")).await;
    let completed = completed(&events);

    assert_eq!(
        completed["response"]["output"][0]["summary"],
        json!([{"type": "summary_text", "text": "alias field"}])
    );
}

#[tokio::test]
async fn tool_call_stream_emits_function_call_and_filters_denied_tools() {
    let (upstream_port, bodies) =
        spawn_mock_upstream(vec![fixture_text("glm_streams/tool_call.sse")]).await;
    let relay = Relay::spawn(&format!("http://127.0.0.1:{upstream_port}/v1"));

    let events = post_stream_events(&relay, fixture_json("tool_call_request.json")).await;
    let completed = completed(&events);
    let item = &completed["response"]["output"][0];
    assert_eq!(item["type"], "function_call");
    assert_eq!(item["name"], "exec_command");
    assert_eq!(item["call_id"], "call_exec");
    assert_eq!(item["arguments"], "{\"cmd\":\"pwd\"}");

    let request_bodies = bodies.lock().unwrap();
    let tool_names: Vec<&str> = request_bodies[0]["tools"]
        .as_array()
        .expect("tools array")
        .iter()
        .filter_map(|tool| tool["function"]["name"].as_str())
        .collect();
    assert_eq!(tool_names, vec!["exec_command", "mcp__node_repl-js"]);
}

#[tokio::test]
async fn two_turn_tool_round_trip_sends_tool_result_without_duplicate_call() {
    let (upstream_port, bodies) = spawn_mock_upstream(vec![
        fixture_text("glm_streams/tool_call.sse"),
        fixture_text("glm_streams/text.sse"),
    ])
    .await;
    let relay = Relay::spawn(&format!("http://127.0.0.1:{upstream_port}/v1"));

    let first_events = post_stream_events(&relay, fixture_json("tool_call_request.json")).await;
    let first_completed = completed(&first_events);
    let response_id = first_completed["response"]["id"]
        .as_str()
        .expect("response id")
        .to_string();

    let mut followup = fixture_json("tool_output_followup.json");
    followup["previous_response_id"] = Value::String(response_id);
    let _second_events = post_stream_events(&relay, followup).await;

    let request_bodies = bodies.lock().unwrap();
    assert_eq!(request_bodies.len(), 2);
    let messages = request_bodies[1]["messages"]
        .as_array()
        .expect("second upstream messages");
    let tool_messages: Vec<&Value> = messages
        .iter()
        .filter(|msg| msg["role"] == "tool")
        .collect();
    assert_eq!(tool_messages.len(), 1);
    assert_eq!(tool_messages[0]["tool_call_id"], "call_exec");
    assert_eq!(
        tool_messages[0]["content"],
        "{\"stdout\":\"/tmp/project\\n\",\"exit_code\":0}"
    );

    let assistant_call_count = messages
        .iter()
        .filter_map(|msg| msg["tool_calls"].as_array())
        .flat_map(|calls| calls.iter())
        .filter(|call| call["id"] == "call_exec")
        .count();
    assert_eq!(
        assistant_call_count, 1,
        "function call should not be duplicated"
    );
}

#[tokio::test]
async fn upstream_error_emits_response_failed() {
    let (upstream_port, _bodies) =
        spawn_mock_upstream_with(Vec::new(), StatusCode::BAD_GATEWAY).await;
    let relay = Relay::spawn(&format!("http://127.0.0.1:{upstream_port}/v1"));

    let events = post_stream_events(&relay, fixture_json("text_only.json")).await;
    let failed = failed(&events);
    assert_eq!(failed["response"]["status"], "failed");
    assert_eq!(failed["response"]["error"]["code"], "502");
}

#[tokio::test]
async fn incomplete_stream_without_content_fails() {
    let (upstream_port, _bodies) =
        spawn_mock_upstream(vec![fixture_text("glm_streams/incomplete_no_content.sse")]).await;
    let relay = Relay::spawn(&format!("http://127.0.0.1:{upstream_port}/v1"));

    let events = post_stream_events(&relay, fixture_json("text_only.json")).await;
    let failed = failed(&events);
    assert_eq!(failed["response"]["status"], "failed");
    assert_eq!(failed["response"]["error"]["code"], "stream_incomplete");
}
