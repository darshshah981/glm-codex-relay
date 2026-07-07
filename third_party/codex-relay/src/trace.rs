use crate::types::{ChatMessage, ChatRequest, ChatUsage, ResponsesInput, ResponsesRequest};
use serde_json::{json, Value};
use std::{
    fs::{create_dir_all, File, OpenOptions},
    io::{Error, ErrorKind, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};
use tracing::warn;

const TRACE_SCHEMA_VERSION: &str = "glm-relay-trace/v1";
const TEXT_PREVIEW_CHARS: usize = 4000;

#[derive(Clone, Debug, Default)]
pub struct TraceSink {
    root: Option<Arc<PathBuf>>,
    active_worker_file: Option<Arc<PathBuf>>,
}

#[derive(Clone, Debug)]
pub struct TraceHandle {
    inner: Option<Arc<TraceHandleInner>>,
}

#[derive(Debug)]
struct TraceHandleInner {
    trace_id: String,
    run_id: String,
    path: PathBuf,
    seq: Mutex<u64>,
    file: Mutex<Option<File>>,
    worker: Option<Value>,
}

impl TraceSink {
    pub fn new(root: Option<PathBuf>, active_worker_file: Option<PathBuf>) -> Self {
        Self {
            root: root.map(Arc::new),
            active_worker_file: active_worker_file.map(Arc::new),
        }
    }

    pub fn from_env() -> Self {
        let root = std::env::var_os("CODEX_RELAY_TRACE_DIR")
            .map(PathBuf::from)
            .filter(|path| !path.as_os_str().is_empty());
        let active_worker_file = std::env::var_os("CODEX_RELAY_ACTIVE_WORKER_FILE")
            .map(PathBuf::from)
            .filter(|path| !path.as_os_str().is_empty());
        Self::new(root, active_worker_file)
    }

    pub fn enabled(&self) -> bool {
        self.root.is_some()
    }

    pub fn start_trace(&self) -> TraceHandle {
        let Some(root) = &self.root else {
            return TraceHandle { inner: None };
        };

        let trace_id = format!("trace_{}", uuid::Uuid::new_v4().simple());
        let worker = self
            .active_worker_file
            .as_deref()
            .and_then(|path| read_worker_marker(path).ok().flatten());
        let run_id = worker
            .as_ref()
            .and_then(|value| value.get("run_id"))
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .unwrap_or("orphan")
            .to_string();
        let safe_run_id = safe_path_component(&run_id);
        let path = root
            .join("runs")
            .join(safe_run_id)
            .join(format!("{trace_id}.jsonl"));

        TraceHandle {
            inner: Some(Arc::new(TraceHandleInner {
                trace_id,
                run_id,
                path,
                seq: Mutex::new(0),
                file: Mutex::new(None),
                worker,
            })),
        }
    }
}

impl TraceHandle {
    pub fn disabled() -> Self {
        Self { inner: None }
    }

    pub fn enabled(&self) -> bool {
        self.inner.is_some()
    }

    pub fn run_id(&self) -> Option<&str> {
        self.inner.as_ref().map(|inner| inner.run_id.as_str())
    }

    pub fn trace_id(&self) -> Option<&str> {
        self.inner.as_ref().map(|inner| inner.trace_id.as_str())
    }

    pub fn path(&self) -> Option<&Path> {
        self.inner.as_ref().map(|inner| inner.path.as_path())
    }

    pub fn worker_context(&self) -> Option<&Value> {
        self.inner.as_ref().and_then(|inner| inner.worker.as_ref())
    }

    pub fn emit(&self, event: &str, data: Value) {
        if let Err(e) = self.try_emit(event, data) {
            warn!("trace write failed: {e}");
        }
    }

    pub fn try_emit(&self, event: &str, data: Value) -> std::io::Result<()> {
        let Some(inner) = &self.inner else {
            return Ok(());
        };

        let seq = {
            let mut seq = inner
                .seq
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            *seq += 1;
            *seq
        };

        let payload = json!({
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": &inner.trace_id,
            "run_id": &inner.run_id,
            "seq": seq,
            "ts_unix_ms": unix_time_ms(),
            "event": event,
            "data": data,
        });

        let mut file = inner
            .file
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if file.is_none() {
            if let Some(parent) = inner.path.parent() {
                create_dir_all(parent)?;
            }
            *file = Some(
                OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&inner.path)?,
            );
        }
        let file = file.as_mut().expect("trace file should be initialized");
        serde_json::to_writer(&mut *file, &payload).map_err(|e| Error::new(ErrorKind::Other, e))?;
        file.write_all(b"\n")?;
        file.flush()?;
        Ok(())
    }
}

pub fn responses_request_summary(req: &ResponsesRequest, tool_names: Vec<String>) -> Value {
    json!({
        "model": &req.model,
        "stream": req.stream,
        "previous_response_id": req.previous_response_id.as_deref(),
        "temperature": req.temperature,
        "max_output_tokens": req.max_output_tokens,
        "system": req.system.as_deref().map(text_summary),
        "instructions": req.instructions.as_deref().map(text_summary),
        "input": responses_input_summary(&req.input),
        "tool_count": req.tools.len(),
        "tool_names": tool_names,
    })
}

pub fn chat_request_summary(chat_req: &ChatRequest, upstream_body: &Value) -> Value {
    let upstream_keys = upstream_body
        .as_object()
        .map(|object| object.keys().cloned().collect::<Vec<_>>())
        .unwrap_or_default();

    json!({
        "model": &chat_req.model,
        "stream": chat_req.stream,
        "message_count": chat_req.messages.len(),
        "messages": chat_req.messages.iter().map(chat_message_summary).collect::<Vec<_>>(),
        "tool_count": chat_req.tools.len(),
        "tool_names": chat_req.tools.iter().filter_map(chat_tool_name).collect::<Vec<_>>(),
        "temperature": chat_req.temperature,
        "max_tokens": chat_req.max_tokens,
        "thinking_enabled": chat_req.thinking.is_some(),
        "stream_options": chat_req.stream_options.is_some(),
        "upstream_keys": upstream_keys,
    })
}

pub fn chat_response_summary(message: &ChatMessage, usage: Option<&ChatUsage>) -> Value {
    json!({
        "message": chat_message_summary(message),
        "tool_names": message.tool_calls.as_ref().map(|calls| {
            calls.iter().filter_map(chat_response_tool_name).collect::<Vec<_>>()
        }).unwrap_or_default(),
        "usage": usage_summary(usage),
    })
}

pub fn usage_summary(usage: Option<&ChatUsage>) -> Value {
    match usage {
        Some(usage) => json!({
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cached_tokens": usage.cache_hit(),
        }),
        None => Value::Null,
    }
}

pub fn text_delta(text: &str) -> Value {
    json!({
        "text": text,
        "chars": text.chars().count(),
    })
}

pub fn tool_call_delta(index: usize, id: &str, name: &str, arguments_delta: &str) -> Value {
    json!({
        "index": index,
        "id": id,
        "name": name,
        "arguments_delta": arguments_delta,
        "arguments_delta_chars": arguments_delta.chars().count(),
    })
}

fn responses_input_summary(input: &ResponsesInput) -> Value {
    match input {
        ResponsesInput::Text(text) => json!({
            "kind": "text",
            "text": text_summary(text),
        }),
        ResponsesInput::Messages(items) => json!({
            "kind": "messages",
            "count": items.len(),
            "items": items.iter().map(response_item_summary).collect::<Vec<_>>(),
        }),
    }
}

fn response_item_summary(item: &Value) -> Value {
    let content = match item.get("content") {
        Some(Value::String(text)) => json!({"kind": "text", "text": text_summary(text)}),
        Some(Value::Array(parts)) => json!({
            "kind": "parts",
            "parts": parts.iter().map(response_content_part_summary).collect::<Vec<_>>(),
        }),
        Some(other) => json!({"kind": value_kind(other)}),
        None => Value::Null,
    };

    json!({
        "type": item.get("type").and_then(Value::as_str),
        "role": item.get("role").and_then(Value::as_str),
        "call_id": item.get("call_id").and_then(Value::as_str),
        "name": item.get("name").and_then(Value::as_str),
        "content": content,
    })
}

fn response_content_part_summary(part: &Value) -> Value {
    json!({
        "type": part.get("type").and_then(Value::as_str),
        "text": part.get("text").and_then(Value::as_str).map(text_summary),
    })
}

fn chat_message_summary(message: &ChatMessage) -> Value {
    json!({
        "role": &message.role,
        "content": message.content.as_ref().map(value_text_summary).unwrap_or(Value::Null),
        "reasoning": message.reasoning_content.as_deref().map(text_summary),
        "tool_call_id": message.tool_call_id.as_deref(),
        "name": message.name.as_deref(),
        "tool_call_count": message.tool_calls.as_ref().map(Vec::len).unwrap_or(0),
    })
}

fn value_text_summary(value: &Value) -> Value {
    match value {
        Value::String(text) => json!({"kind": "text", "text": text_summary(text)}),
        Value::Array(parts) => json!({
            "kind": "parts",
            "parts": parts.iter().map(response_content_part_summary).collect::<Vec<_>>(),
        }),
        other => json!({"kind": value_kind(other)}),
    }
}

fn text_summary(text: &str) -> Value {
    let chars = text.chars().count();
    let preview: String = text.chars().take(TEXT_PREVIEW_CHARS).collect();
    json!({
        "chars": chars,
        "preview": preview,
        "truncated": chars > TEXT_PREVIEW_CHARS,
    })
}

fn chat_tool_name(tool: &Value) -> Option<String> {
    tool.get("function")
        .and_then(|function| function.get("name"))
        .and_then(Value::as_str)
        .or_else(|| tool.get("name").and_then(Value::as_str))
        .map(String::from)
}

fn chat_response_tool_name(tool_call: &Value) -> Option<String> {
    tool_call
        .get("function")
        .and_then(|function| function.get("name"))
        .and_then(Value::as_str)
        .map(String::from)
}

fn value_kind(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn read_worker_marker(path: &Path) -> std::io::Result<Option<Value>> {
    if !path.exists() {
        return Ok(None);
    }
    let text = std::fs::read_to_string(path)?;
    match serde_json::from_str::<Value>(&text) {
        Ok(value @ Value::Object(_)) => Ok(Some(value)),
        Ok(_) | Err(_) => Ok(None),
    }
}

fn safe_path_component(value: &str) -> String {
    let mut out = String::new();
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' || ch == '.' {
            out.push(ch);
        } else {
            out.push('-');
        }
    }
    let out = out.trim_matches('-');
    if out.is_empty() {
        "orphan".to_string()
    } else {
        out.to_string()
    }
}

fn unix_time_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn disabled_sink_writes_nothing() {
        let sink = TraceSink::new(None, None);
        let trace = sink.start_trace();

        assert!(!trace.enabled());
        trace.emit("test.event", json!({"ok": true}));
    }

    #[test]
    fn enabled_sink_writes_ordered_events_with_worker_marker() {
        let temp = std::env::temp_dir().join(format!(
            "codex-relay-trace-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        let root = temp.join("traces");
        let marker = temp.join("active-worker.json");
        create_dir_all(&temp).unwrap();
        std::fs::write(
            &marker,
            json!({
                "run_id": "20260707-demo",
                "run_dir": "outputs/glm-worker-runs/20260707-demo",
                "prompt_path": "outputs/glm-worker-runs/20260707-demo/prompt.txt"
            })
            .to_string(),
        )
        .unwrap();

        let sink = TraceSink::new(Some(root), Some(marker));
        let trace = sink.start_trace();
        trace.try_emit("first", json!({"value": 1})).unwrap();
        trace.try_emit("second", json!({"value": 2})).unwrap();

        let path = trace.path().unwrap();
        let lines = std::fs::read_to_string(path).unwrap();
        let events: Vec<Value> = lines
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["schema_version"], TRACE_SCHEMA_VERSION);
        assert_eq!(events[0]["run_id"], "20260707-demo");
        assert_eq!(events[0]["seq"], 1);
        assert_eq!(events[1]["seq"], 2);
        assert_eq!(trace.worker_context().unwrap()["run_id"], "20260707-demo");

        let _ = std::fs::remove_dir_all(temp);
    }

    #[test]
    fn malformed_marker_falls_back_to_orphan() {
        let temp = std::env::temp_dir().join(format!(
            "codex-relay-trace-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        let root = temp.join("traces");
        let marker = temp.join("active-worker.json");
        create_dir_all(&temp).unwrap();
        std::fs::write(&marker, "not-json").unwrap();

        let sink = TraceSink::new(Some(root), Some(marker));
        let trace = sink.start_trace();

        assert_eq!(trace.run_id(), Some("orphan"));
        let _ = std::fs::remove_dir_all(temp);
    }
}
