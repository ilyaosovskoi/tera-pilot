# Tera Pilot — developer convenience targets.
#
# The most useful ones:
#   make native        — build + install the Rust acceleration (one command)
#   make test          — run the full test suite
#   make test-native   — run just the native-extension tests

.PHONY: native native-install native-test test test-native doctor

## Build and install the Rust native extension (tera_pilot_native).
## One command — requires a Rust toolchain (rustc + cargo) and maturin:
##   pip install maturin        (or: pip install -e .[dev])
native: native-install

native-install:
	@echo "==> Building tera_pilot_native (release)…"
	cd tera-pilot-native && maturin build --release --manifest-path pyo3/Cargo.toml
	@echo "==> Installing wheel…"
	python3 -m pip install --force-reinstall tera-pilot-native/target/wheels/*.whl
	@echo "==> Verifying…"
	python3 -c "from tera_pilot.agent import native; assert native.NATIVE_AVAILABLE, 'native not loaded'; print('tera_pilot_native', native.native_version(), 'OK')"

native-test:
	python3 -m pytest tests/test_native.py -q

test:
	python3 -m pytest -q

test-native: native-test

doctor:
	python3 -m tera_pilot.cli doctor
