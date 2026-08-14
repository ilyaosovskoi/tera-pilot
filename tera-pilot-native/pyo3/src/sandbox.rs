//! Sandbox submodule — profile state machine + fast advisory path checks.
//!
//! Mirrors `tera_pilot/agent/_fallback_sandbox.py` exactly in behavior.
//! The hot path (`path_would_be_writable`) is called for every file write
//! the agent attempts, so it runs in native code: canonical path resolution
//! (symlinks + `..` handled like Python's `Path.resolve()`) followed by a
//! component-wise prefix comparison instead of repeated Python string/Path
//! object churn.
//!
//! NOTE: kernel-level enforcement (Landlock/Seatbelt) is NOT wired yet —
//! `supported_platform()` returns False, same contract as the fallback.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyModule;
use std::path::{Component, Path, PathBuf};
use std::sync::{Mutex, OnceLock};

const PROFILE_OFF: &str = "off";
const PROFILE_WORKSPACE: &str = "workspace";
const PROFILE_READ_ONLY: &str = "read-only";
const PROFILE_STRICT: &str = "strict";
const VALID_PROFILES: [&str; 4] = [PROFILE_OFF, PROFILE_WORKSPACE, PROFILE_READ_ONLY, PROFILE_STRICT];

#[derive(Clone, Debug)]
struct AppliedSandbox {
    profile: String,
}

static STATE: OnceLock<Mutex<Option<AppliedSandbox>>> = OnceLock::new();

fn state() -> &'static Mutex<Option<AppliedSandbox>> {
    STATE.get_or_init(|| Mutex::new(None))
}

/// Lexically normalize `.` / `..` components the way Python's
/// `Path.resolve()` does for paths with non-existent parts.
fn normalize_lexically(path: &Path) -> PathBuf {
    let mut out: Vec<Component<'_>> = Vec::new();
    for comp in path.components() {
        match comp {
            Component::CurDir => {}
            Component::ParentDir => {
                if let Some(Component::Normal(_)) = out.last() {
                    out.pop();
                }
                // ".." у корня не даёт уйти выше — игнорируем (как Python)
            }
            other => out.push(other),
        }
    }
    out.into_iter().collect()
}

/// Resolve a path the way Python's `Path.resolve()` does (strict=False):
/// symlink resolution for the deepest existing ancestor + lexical
/// normalization of `.` / `..` for the remainder. Never fails.
///
/// How this stays faithful to Python:
/// - `Path::exists()` lets the OS resolve `..` and symlinks against the
///   existing prefix, so the walk stops at exactly the deepest resolved
///   ancestor Python's `realpath` would reach;
/// - the remainder is collected WITH its `..` components (via
///   `components().next_back()` — `Path::file_name()` returns `None` for a
///   trailing `..` and would silently drop the escape);
/// - after appending the remainder to the canonicalized ancestor,
///   `normalize_lexically` collapses `.` / `..`, exactly like Python.
///
/// `std::path::absolute` is deliberately not used: it does not normalize
/// `..` at all, which let `<ws>/sub/../../etc/passwd` escape detection.
fn resolve_path(p: &str) -> PathBuf {
    let path = Path::new(p);
    if let Ok(c) = std::fs::canonicalize(path) {
        return c;
    }
    // Build an absolute path ourselves.
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        match std::env::current_dir() {
            Ok(cwd) => cwd.join(path),
            Err(_) => return path.to_path_buf(),
        }
    };
    // Walk up to the deepest existing ancestor, remembering the remainder
    // (including `..` components).
    let mut existing = abs.as_path();
    let mut suffix: Vec<PathBuf> = Vec::new();
    loop {
        if existing.as_os_str().is_empty() || existing.exists() {
            break;
        }
        if let Some(comp) = existing.components().next_back() {
            match comp {
                Component::Normal(n) => suffix.push(PathBuf::from(n)),
                Component::ParentDir => suffix.push(PathBuf::from("..")),
                _ => {}
            }
        }
        match existing.parent() {
            Some(p) => existing = p,
            None => break,
        }
    }
    let mut base = std::fs::canonicalize(existing).unwrap_or_else(|_| existing.to_path_buf());
    for part in suffix.iter().rev() {
        base.push(part);
    }
    // Collapse `.` / `..` now that symlinks in the existing prefix are resolved.
    normalize_lexically(&base)
}

/// Component-wise `is_relative_to` on canonical absolute paths.
fn is_relative_to(path: &Path, base: &Path) -> bool {
    let mut iter = path.components();
    for b in base.components() {
        match iter.next() {
            Some(c) if c == b => continue,
            _ => return false,
        }
    }
    true
}

#[pyfunction]
#[pyo3(signature = (
    profile,
    workspace_root = None,
    allowed_egress = None,
    extra_readonly_paths = None,
    extra_readwrite_paths = None,
))]
#[allow(unused_variables)]
fn apply_profile(
    py: Python<'_>,
    profile: String,
    workspace_root: Option<String>,
    allowed_egress: Option<Vec<String>>,
    extra_readonly_paths: Option<Vec<String>>,
    extra_readwrite_paths: Option<Vec<String>>,
) -> PyResult<()> {
    let _ = py;
    if !VALID_PROFILES.contains(&profile.as_str()) {
        return Err(PyValueError::new_err(format!("invalid sandbox profile: {profile:?}")));
    }
    let mut st = state().lock().unwrap();
    if let Some(applied) = st.as_ref() {
        if applied.profile != PROFILE_OFF {
            return Err(PyRuntimeError::new_err(format!(
                "sandbox already applied (profile={:?}); restrictions are irreversible",
                applied.profile
            )));
        }
    }
    *st = Some(AppliedSandbox { profile });
    Ok(())
}

#[pyfunction]
fn current_profile() -> Option<String> {
    state().lock().unwrap().as_ref().map(|a| a.profile.clone())
}

#[pyfunction]
fn describe_state() -> String {
    match state().lock().unwrap().as_ref() {
        None => "not applied".to_string(),
        Some(a) => format!("applied (profile={})", a.profile),
    }
}

#[pyfunction]
#[pyo3(signature = (profile, workspace_root, path, extra_readwrite_paths = None))]
fn path_would_be_writable(
    profile: String,
    workspace_root: Option<String>,
    path: String,
    extra_readwrite_paths: Option<Vec<String>>,
) -> bool {
    let path = resolve_path(&path);

    if profile == PROFILE_READ_ONLY || profile == PROFILE_STRICT {
        // Only paths explicitly listed as read-write are writable.
        for p in extra_readwrite_paths.as_deref().unwrap_or(&[]) {
            if is_relative_to(&path, &resolve_path(p)) {
                return true;
            }
        }
        return false;
    }
    if let Some(ws) = workspace_root.as_deref() {
        if is_relative_to(&path, &resolve_path(ws)) {
            return true;
        }
    }
    for p in extra_readwrite_paths.as_deref().unwrap_or(&[]) {
        if is_relative_to(&path, &resolve_path(p)) {
            return true;
        }
    }
    false
}

#[pyfunction]
fn supported_platform() -> bool {
    // Kernel-level enforcement is not wired in the extension yet.
    false
}

pub fn init(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(apply_profile, m)?)?;
    m.add_function(wrap_pyfunction!(current_profile, m)?)?;
    m.add_function(wrap_pyfunction!(describe_state, m)?)?;
    m.add_function(wrap_pyfunction!(path_would_be_writable, m)?)?;
    m.add_function(wrap_pyfunction!(supported_platform, m)?)?;
    Ok(())
}
