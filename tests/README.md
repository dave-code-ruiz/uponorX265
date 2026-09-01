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

## Why the HA version is pinned

`requirements_test.txt` pins `pytest-homeassistant-custom-component==0.13.357`
(Home Assistant 2026.8.3), the newest release tracking a *stable* HA. Later
releases pull the 2026.9 betas, which promote `device_registry.async_get_device`
from a deprecation warning to a hard `RuntimeError`. The integration calls it at
[`__init__.py:269`](../custom_components/uponorx265/__init__.py#L269), and the
device-registry tests call it directly, so 11 tests fail on the beta.

Migrating those calls to `async_get_device_by_identifier` is what unblocks the
pin — that work is not done yet.
