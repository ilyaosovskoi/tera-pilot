//! Interjection submodule — thread-safe FIFO buffer for mid-turn user
//! messages. Mirrors `tera_pilot/agent/_fallback_interjection.py`.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule};
use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

const LARGE_PROMPT_THRESHOLD: usize = 25_000;

struct Entry {
    id: i64,
    received_at_unix_millis: i64,
    text: String,
    attachment: Option<String>,
}

/// UTF-8-safe truncation by code points (matches Python `s[:max_chars]`).
fn truncate_utf8_safe(s: &str, max_chars: usize) -> (String, bool) {
    let count = s.chars().count();
    if count <= max_chars {
        (s.to_string(), false)
    } else {
        (s.chars().take(max_chars).collect(), true)
    }
}

fn render_text(raw: &str, truncated: bool) -> String {
    let body = if truncated {
        format!("{raw}\n\n[truncated — original was {} chars]", raw.chars().count())
    } else {
        raw.to_string()
    };
    format!("The user sent a message while you were working:\n<user_query>\n{body}\n</user_query>")
}

fn now_millis() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

#[pyclass]
struct InterjectionBuffer {
    queue: Mutex<VecDeque<Entry>>,
    next_id: Mutex<i64>,
}

#[pymethods]
impl InterjectionBuffer {
    #[new]
    fn new() -> Self {
        Self {
            queue: Mutex::new(VecDeque::new()),
            next_id: Mutex::new(1),
        }
    }

    #[pyo3(signature = (text, attachment = None))]
    fn push(&self, text: String, attachment: Option<String>) -> i64 {
        let mut q = self.queue.lock().unwrap();
        let mut nid = self.next_id.lock().unwrap();
        let id = *nid;
        *nid += 1;
        q.push_back(Entry {
            id,
            received_at_unix_millis: now_millis(),
            text,
            attachment,
        });
        id
    }

    fn drain<'py>(&self, py: Python<'py>) -> PyResult<Vec<Py<PyDict>>> {
        let mut q = self.queue.lock().unwrap();
        let items: Vec<Entry> = q.drain(..).collect();
        let mut out = Vec::with_capacity(items.len());
        for it in items {
            let (raw, truncated) = truncate_utf8_safe(&it.text, LARGE_PROMPT_THRESHOLD);
            let d = PyDict::new(py);
            d.set_item("id", it.id)?;
            d.set_item("received_at_unix_millis", it.received_at_unix_millis)?;
            d.set_item("raw_text", raw)?;
            d.set_item("truncated", truncated)?;
            d.set_item("attachment", it.attachment)?;
            out.push(d.unbind());
        }
        Ok(out)
    }

    fn drain_formatted(&self) -> Option<String> {
        let mut q = self.queue.lock().unwrap();
        let items: Vec<Entry> = q.drain(..).collect();
        if items.is_empty() {
            return None;
        }
        let parts: Vec<String> = items
            .iter()
            .map(|it| {
                let (raw, truncated) = truncate_utf8_safe(&it.text, LARGE_PROMPT_THRESHOLD);
                render_text(&raw, truncated)
            })
            .collect();
        Some(parts.join("\n\n").trim().to_string())
    }

    fn pending_count(&self) -> usize {
        self.queue.lock().unwrap().len()
    }
}

#[pyfunction]
fn render_entry(text: String, truncated: bool) -> String {
    render_text(&text, truncated)
}

pub fn init(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<InterjectionBuffer>()?;
    m.add_function(wrap_pyfunction!(render_entry, m)?)?;
    Ok(())
}
