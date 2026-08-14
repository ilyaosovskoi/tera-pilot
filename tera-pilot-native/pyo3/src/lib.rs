//! `tera_pilot_native` — Rust acceleration for Tera Pilot (PyO3).
//!
//! The extension exposes five submodules that mirror the pure-Python
//! fallbacks in `tera_pilot/agent/_fallback_*`:
//!
//! - `sandbox`         — profile state machine + fast advisory path checks
//! - `circuit_breaker` — sliding-window error-rate breaker per provider key
//! - `interjection`    — thread-safe FIFO mid-turn message buffer
//! - `compaction`      — three-tier (code/intra/inter) compaction engine
//! - `actor`           — CancelToken with parent→child cancellation
//!
//! Loader: `tera_pilot.agent.native` (falls back to pure Python if this
//! extension is not installed).

use pyo3::prelude::*;
use pyo3::types::PyModule;

mod actor;
mod circuit_breaker;
mod compaction;
mod interjection;
mod sandbox;

#[pymodule]
fn tera_pilot_native(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;

    let sandbox_mod = PyModule::new(py, "sandbox")?;
    sandbox::init(py, &sandbox_mod)?;
    m.add_submodule(&sandbox_mod)?;

    let cb_mod = PyModule::new(py, "circuit_breaker")?;
    circuit_breaker::init(py, &cb_mod)?;
    m.add_submodule(&cb_mod)?;

    let ij_mod = PyModule::new(py, "interjection")?;
    interjection::init(py, &ij_mod)?;
    m.add_submodule(&ij_mod)?;

    let comp_mod = PyModule::new(py, "compaction")?;
    compaction::init(py, &comp_mod)?;
    m.add_submodule(&comp_mod)?;

    let actor_mod = PyModule::new(py, "actor")?;
    actor::init(py, &actor_mod)?;
    m.add_submodule(&actor_mod)?;

    Ok(())
}
