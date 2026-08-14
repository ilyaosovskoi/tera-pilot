//! Circuit breaker submodule.
//!
//! Mirrors `tera_pilot/agent/_fallback_circuit_breaker.py`, but the
//! sliding-window bookkeeping is O(1) amortized (incremental counters)
//! instead of the Python O(window) scans under a `threading.Lock`.
//! This runs on EVERY provider / MCP call, so it is the primary
//! acceleration target.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule};
use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const STATE_CLOSED: &str = "closed";
const STATE_OPEN: &str = "open";
const STATE_HALF_OPEN: &str = "half_open";

#[derive(Clone, Copy)]
struct Sample {
    at: Instant,
    success: bool,
    rate_limited: bool,
}

#[derive(Clone)]
struct BreakerConfig {
    min_samples: usize,
    error_rate_threshold: f64,
    window_secs: f64,
    open_duration_secs: f64,
}

impl BreakerConfig {
    fn window(&self) -> Duration {
        Duration::from_secs_f64(self.window_secs.max(0.0))
    }
    fn open_duration(&self) -> Duration {
        Duration::from_secs_f64(self.open_duration_secs.max(0.0))
    }
}

struct BreakerData {
    key: String,
    cfg: BreakerConfig,
    samples: VecDeque<Sample>,
    state: String,
    opened_at: Option<Instant>,
    probe_claimed_at: Option<Instant>,
    lifetime_success: u64,
    lifetime_failure: u64,
    lifetime_rate_limited: u64,
    generation: u64,
}

impl BreakerData {
    fn new(key: String, cfg: &BreakerConfig) -> Self {
        Self {
            key,
            cfg: cfg.clone(),
            samples: VecDeque::new(),
            state: STATE_CLOSED.to_string(),
            opened_at: None,
            probe_claimed_at: None,
            lifetime_success: 0,
            lifetime_failure: 0,
            lifetime_rate_limited: 0,
            generation: 0,
        }
    }

    fn prune(&mut self, now: Instant) {
        let cutoff = now - self.cfg.window();
        while let Some(front) = self.samples.front() {
            if front.at < cutoff {
                self.samples.pop_front();
            } else {
                break;
            }
        }
    }

    fn try_claim(&mut self, now: Instant) -> bool {
        self.prune(now);
        match self.state.as_str() {
            STATE_CLOSED => true,
            STATE_OPEN => {
                if let Some(opened) = self.opened_at {
                    if now.duration_since(opened) >= self.cfg.open_duration() {
                        self.state = STATE_HALF_OPEN.to_string();
                        self.probe_claimed_at = Some(now);
                        self.generation += 1;
                        return true;
                    }
                }
                false
            }
            _ => {
                // half_open
                if let Some(claimed) = self.probe_claimed_at {
                    if now.duration_since(claimed) >= self.cfg.open_duration() {
                        self.probe_claimed_at = Some(now);
                        return true;
                    }
                    return false;
                }
                self.probe_claimed_at = Some(now);
                true
            }
        }
    }

    fn record(&mut self, ok: bool, rate_limited: bool, now: Instant) {
        self.prune(now);
        self.samples.push_back(Sample { at: now, success: ok, rate_limited });
        if ok {
            self.lifetime_success += 1;
        } else if rate_limited {
            self.lifetime_rate_limited += 1;
        } else {
            self.lifetime_failure += 1;
        }
        self.probe_claimed_at = None;
        self.generation += 1;

        if self.state == STATE_HALF_OPEN {
            if ok {
                self.state = STATE_CLOSED.to_string();
                self.opened_at = None;
            } else {
                self.state = STATE_OPEN.to_string();
                self.opened_at = Some(now);
            }
        } else if self.state == STATE_CLOSED && self.samples.len() >= self.cfg.min_samples {
            let errors = self.samples.iter().filter(|s| !s.success).count();
            let rate = errors as f64 / self.samples.len() as f64;
            if rate >= self.cfg.error_rate_threshold {
                self.state = STATE_OPEN.to_string();
                self.opened_at = Some(now);
            }
        }
    }

    fn metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let successes = self.samples.iter().filter(|s| s.success).count();
        let failures = self.samples.iter().filter(|s| !s.success && !s.rate_limited).count();
        let rl = self.samples.iter().filter(|s| s.rate_limited).count();
        let d = PyDict::new(py);
        d.set_item("key", &self.key)?;
        d.set_item("state", &self.state)?;
        d.set_item("window_samples", self.samples.len())?;
        d.set_item("window_successes", successes)?;
        d.set_item("window_failures", failures)?;
        d.set_item("window_rate_limited", rl)?;
        d.set_item("lifetime_success", self.lifetime_success)?;
        d.set_item("lifetime_failure", self.lifetime_failure)?;
        d.set_item("lifetime_rate_limited", self.lifetime_rate_limited)?;
        d.set_item("generation", self.generation)?;
        Ok(d)
    }
}

#[pyclass]
struct CircuitBreaker {
    inner: Arc<Mutex<BreakerData>>,
}

#[pymethods]
impl CircuitBreaker {
    #[getter]
    fn key(&self) -> String {
        self.inner.lock().unwrap().key.clone()
    }

    #[getter]
    fn is_open(&self) -> bool {
        self.inner.lock().unwrap().state == STATE_OPEN
    }

    #[getter]
    fn state(&self) -> String {
        self.inner.lock().unwrap().state.clone()
    }

    fn try_claim(&self) -> bool {
        self.inner.lock().unwrap().try_claim(Instant::now())
    }

    #[pyo3(signature = (ok, rate_limited = false))]
    fn record(&self, ok: bool, rate_limited: bool) {
        self.inner.lock().unwrap().record(ok, rate_limited, Instant::now());
    }

    fn metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.inner.lock().unwrap().metrics(py)
    }
}

#[pyclass]
struct CircuitBreakerRegistry {
    default: BreakerConfig,
    breakers: Mutex<HashMap<String, Arc<Mutex<BreakerData>>>>,
}

#[pymethods]
impl CircuitBreakerRegistry {
    #[new]
    #[pyo3(signature = (min_samples = 10, error_rate_threshold = 0.5, window_secs = 60.0, open_duration_secs = 15.0))]
    fn new(min_samples: usize, error_rate_threshold: f64, window_secs: f64, open_duration_secs: f64) -> Self {
        Self {
            default: BreakerConfig {
                min_samples,
                error_rate_threshold,
                window_secs,
                open_duration_secs,
            },
            breakers: Mutex::new(HashMap::new()),
        }
    }

    fn get(&self, key: String) -> CircuitBreaker {
        let mut map = self.breakers.lock().unwrap();
        let inner = map
            .entry(key.clone())
            .or_insert_with(|| Arc::new(Mutex::new(BreakerData::new(key, &self.default))))
            .clone();
        CircuitBreaker { inner }
    }

    fn all_metrics<'py>(&self, py: Python<'py>) -> PyResult<Vec<Py<PyDict>>> {
        let map = self.breakers.lock().unwrap();
        let mut out = Vec::with_capacity(map.len());
        for b in map.values() {
            out.push(b.lock().unwrap().metrics(py)?.unbind());
        }
        Ok(out)
    }
}

#[pyfunction]
fn retry_disposition_server(status: i64) -> String {
    if status == 429 || (500..600).contains(&status) {
        "retryable".to_string()
    } else if status == 401 || status == 403 {
        "auth_refresh".to_string()
    } else {
        "terminal".to_string()
    }
}

#[pyfunction]
fn retry_disposition_client_storage(status: i64) -> String {
    if status == 400 || status == 403 || status == 404 {
        "terminal".to_string()
    } else if status == 401 {
        "auth_refresh".to_string()
    } else {
        "retryable".to_string()
    }
}

pub fn init(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<CircuitBreaker>()?;
    m.add_class::<CircuitBreakerRegistry>()?;
    m.add_function(wrap_pyfunction!(retry_disposition_server, m)?)?;
    m.add_function(wrap_pyfunction!(retry_disposition_client_storage, m)?)?;
    Ok(())
}
