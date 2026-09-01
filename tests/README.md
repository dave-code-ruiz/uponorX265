# Test environment

## Setup

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-test-env.ps1
```

Add `-Force` to rebuild `.venv` from scratch, or `-PythonVersion 3.13` to build
against a different interpreter (HA 2026.8 supports 3.13 and 3.14).

## Running

```powershell
.venv\Scripts\python.exe -m pytest
```

`pytest.ini` sets `testpaths = tests` and `asyncio_mode = auto`, so a bare
`pytest` runs the whole suite.

## Why running on Windows needs a shim

`pytest-homeassistant-custom-component` imports `homeassistant.runner` while
loading its plugin, and `runner` imports the POSIX-only `fcntl` and `resource`
modules at module scope. On Windows that makes the plugin unimportable, so
pytest dies during plugin loading, before collecting a single test.

`tests/_win_stubs/` holds minimal stand-ins for those two modules. The setup
script puts that directory on the interpreter's path with a `.pth` file in the
venv's `site-packages` — a `.pth` rather than `conftest.py` because plugin entry
points load before any conftest is read, while `.pth` files run at interpreter
startup.

The stubs define only the handful of names `runner` actually touches, so any
other POSIX call reaching through them raises `AttributeError` rather than
quietly succeeding as a no-op. `.pth` entries are appended after the stdlib on
`sys.path`, so on Linux and macOS the real modules still win.

This is only about *importability*. Nothing in the suite runs Home Assistant's
CLI entrypoint, so neither the single-instance lock nor the file-descriptor
limit is ever exercised.

## Which HA version the suite runs against

`requirements_test.txt` no longer pins `pytest-homeassistant-custom-component`.
It was pinned to `0.13.357` (HA 2026.8.3) while the integration still called
`device_registry.async_get_device`, which HA 2026.9 promotes from a deprecation
warning to a hard `RuntimeError`. Those calls now go through the compat shims in
[`__init__.py`](../custom_components/uponorx265/__init__.py), so the suite is
green on the 2026.9 betas and the pin is gone.

### The one thing to watch

The shims prefer the HA 2026.8+ APIs (`async_get_device_by_identifier`,
`async_get_devices`, `via_device_id`) and fall back to the older ones on
earlier cores. Nothing
declares a minimum HA version, so those fallbacks are still live for users on
old cores — and the only thing covering them is
[`test_ha_compat_shims.py`](test_ha_compat_shims.py), which exercises them by
calling the deprecated APIs on purpose.

HA 2026.9 raises on exactly those APIs, so on a 2026.9+ core those three tests
skip themselves rather than fail. That is the honest outcome — the branches are
unreachable there, not broken — but it does mean **a run on latest alone leaves
the fallbacks uncovered**. If you touch the shims, run against both:

```powershell
.venv\Scripts\python.exe -m pytest                                    # latest: 95 passed, 3 skipped
.venv\Scripts\python.exe -m pip install "pytest-homeassistant-custom-component==0.13.357"
.venv\Scripts\python.exe -m pytest                                    # HA 2026.8: 98 passed
```

The fallbacks can be deleted outright — along with those tests — once the
integration declares a minimum of HA 2026.8.
