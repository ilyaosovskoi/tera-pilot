//! Compaction submodule — three-tier conversation compaction engine.
//!
//! Mirrors `tera_pilot/agent/_fallback_compaction.py`. The LLM sampling
//! itself is delegated back to Python via a callable; native code handles
//! chunking, prompt building and history reconstruction.
//!
//! Prompts and messages are byte-for-byte identical to the Python
//! fallback so behavior parity is preserved.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyModule};

#[pyclass(from_py_object)]
#[derive(Clone)]
struct ConversationItem {
    #[pyo3(get)]
    role: String,
    #[pyo3(get)]
    content: String,
    #[pyo3(get)]
    tokens: i64,
}

#[pymethods]
impl ConversationItem {
    #[new]
    #[pyo3(signature = (role, content, tokens = 0))]
    fn new(role: String, content: String, tokens: i64) -> Self {
        Self { role, content, tokens }
    }

    fn count_tokens(&self) -> i64 {
        if self.tokens > 0 {
            self.tokens
        } else {
            (self.content.chars().count() as i64 + 3) / 4
        }
    }
}

#[pyclass]
struct CompactionEngine {
    sampler: Py<PyAny>,
}

fn summary_item(summary: &str, prefix: &str) -> ConversationItem {
    ConversationItem {
        role: "system".to_string(),
        content: format!("{prefix}{summary}"),
        tokens: (summary.chars().count() as i64 + 3) / 4,
    }
}

impl CompactionEngine {
    fn call_sampler<'py>(
        &self,
        py: Python<'py>,
        prompt: &str,
        items: &[ConversationItem],
    ) -> PyResult<String> {
        let list = PyList::empty(py);
        for it in items {
            list.append(Bound::new(py, it.clone())?)?;
        }
        let res = self.sampler.bind(py).call1((prompt, list))?;
        res.extract::<String>()
    }
}

#[pymethods]
impl CompactionEngine {
    #[new]
    fn new(sampler: Py<PyAny>) -> Self {
        Self { sampler }
    }

    fn code_compact<'py>(
        &self,
        py: Python<'py>,
        items: Vec<ConversationItem>,
    ) -> PyResult<(String, Vec<ConversationItem>)> {
        if items.is_empty() {
            return Err(PyValueError::new_err("not enough items to compact"));
        }
        let prompt = "Summarize the following entire conversation. Preserve:\n\
- the user's original goal\n\
- all files modified (paths + intent)\n\
- key decisions made\n\
- errors encountered and how they were resolved\n\
- the current state of progress\n\n\
Keep the summary under 1000 words.";
        let summary = self.call_sampler(py, prompt, &items)?;
        let fresh = vec![summary_item(&summary, "[CONVERSATION SUMMARY]\n")];
        Ok((summary, fresh))
    }

    #[pyo3(signature = (items, keep_recent = 6))]
    fn intra_compact<'py>(
        &self,
        py: Python<'py>,
        items: Vec<ConversationItem>,
        keep_recent: i64,
    ) -> PyResult<(String, Vec<ConversationItem>)> {
        let n = items.len() as i64;
        if n <= keep_recent {
            return Err(PyValueError::new_err(format!(
                "not enough items to compact (got {n}, need {})",
                keep_recent + 1
            )));
        }
        let split = (n - keep_recent) as usize;
        let to_summarize = &items[..split];
        let tail = items[split..].to_vec();

        let prompt = "Summarize the tool-call history of the current turn. Preserve:\n\
- Task/Intent\n- Key Findings\n- Files/Code touched\n\
- Errors/Fixes\n- Actions Taken\n- Current Progress\n\n\
If the tool-call history contains a previous compaction summary, you MUST \
incorporate ALL information from that previous summary. Use internal thinking \
channel. Preserve verbatim data (URLs, file paths, code snippets).";
        let summary = self.call_sampler(py, prompt, to_summarize)?;

        let mut new = vec![summary_item(&summary, "[PREVIOUS TURN SUMMARY]\n")];
        new.extend(tail);
        Ok((summary, new))
    }

    #[pyo3(signature = (items, chunk_size = 10, keep_recent = 6))]
    fn inter_compact<'py>(
        &self,
        py: Python<'py>,
        items: Vec<ConversationItem>,
        chunk_size: i64,
        keep_recent: i64,
    ) -> PyResult<(String, Vec<ConversationItem>)> {
        let n = items.len() as i64;
        if n <= keep_recent + chunk_size {
            return Err(PyValueError::new_err(format!(
                "not enough items to compact (got {n}, need {})",
                keep_recent + chunk_size + 1
            )));
        }
        let split = (n - keep_recent) as usize;
        let to_summarize = &items[..split];
        let tail = items[split..].to_vec();

        let chunk_prompt = "Summarize this conversation chunk concisely (under 200 words). \
Preserve key decisions and file paths.";

        let cs = chunk_size.max(1) as usize;
        let mut summaries: Vec<String> = Vec::new();
        for chunk in to_summarize.chunks(cs) {
            let s = self.call_sampler(py, chunk_prompt, chunk)?;
            summaries.push(s);
        }
        let combined = summaries.join("\n\n---\n\n");
        let mut new = vec![summary_item(&combined, "[CONVERSATION HISTORY SUMMARY]\n")];
        new.extend(tail);
        Ok((combined, new))
    }
}

pub fn init(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<ConversationItem>()?;
    m.add_class::<CompactionEngine>()?;
    Ok(())
}
