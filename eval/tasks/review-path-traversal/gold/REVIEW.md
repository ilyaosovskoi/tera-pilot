# Review findings

serve_file() has a path traversal vulnerability: `filename` is joined
straight onto BASE_DIR without resolving or checking the result, so a
caller can pass `../../etc/passwd` (or an absolute path) and read any
file on the system. Fix: resolve the joined path and verify it is still
inside BASE_DIR (e.g. `full_path.resolve().is_relative_to(BASE_DIR)`),
and reject absolute paths / `..` segments.
