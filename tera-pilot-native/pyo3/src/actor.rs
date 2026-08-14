//! Actor submodule — CancelToken (AbortSignal pattern).
//!
//! Mirrors `tera_pilot/agent/_fallback_actor.py`. Native propagation uses
//! atomic flags (no daemon threads): children register their flag with the
//! parent; cancelling the parent sets all children. `is_cancelled()` is a
//! single atomic load — cheap enough to poll every loop iteration.

use pyo3::prelude::*;
use pyo3::types::PyModule;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};

#[pyclass]
struct CancelToken {
    cancelled: Arc<AtomicBool>,
    reason: Arc<Mutex<Option<String>>>,
    children: Arc<Mutex<Vec<Weak<AtomicBool>>>>,
}

#[pymethods]
impl CancelToken {
    #[new]
    fn new() -> Self {
        Self {
            cancelled: Arc::new(AtomicBool::new(false)),
            reason: Arc::new(Mutex::new(None)),
            children: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }

    /// Cancel with a reason. First reason wins (matching the fallback).
    #[pyo3(signature = (reason = ""))]
    fn cancel(&self, reason: &str) {
        {
            let mut r = self.reason.lock().unwrap();
            if r.is_none() {
                *r = Some(reason.to_string());
            }
        }
        self.cancelled.store(true, Ordering::SeqCst);
        let kids: Vec<Weak<AtomicBool>> = self.children.lock().unwrap().iter().cloned().collect();
        for k in kids {
            if let Some(flag) = k.upgrade() {
                flag.store(true, Ordering::SeqCst);
            }
        }
    }

    #[getter]
    fn reason(&self) -> Option<String> {
        self.reason.lock().unwrap().clone()
    }

    /// Spawn a child token auto-cancelled when this token is cancelled.
    fn child(&self) -> CancelToken {
        let child = CancelToken::new();
        if self.is_cancelled() {
            child.cancel("parent cancelled");
            return child;
        }
        self.children.lock().unwrap().push(Arc::downgrade(&child.cancelled));
        child
    }
}

pub fn init(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<CancelToken>()?;
    Ok(())
}
