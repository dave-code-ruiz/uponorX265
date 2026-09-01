"""Minimal Windows stub for the POSIX-only stdlib `fcntl` module.

`homeassistant.runner` does `import fcntl` at module scope (it uses
`fcntl.flock` for its single-instance lock file), and
`pytest_homeassistant_custom_component.patch_time` imports `runner` at module
scope in turn. That makes the pytest plugin unimportable on Windows, so pytest
dies while loading plugins, before a single test is collected.

Nothing in this suite runs Home Assistant's CLI entrypoint, so the lock is
never taken — these names only need to exist for the import to succeed.

Deliberately minimal: only what `runner` actually touches is defined, so any
other POSIX call that starts reaching through this module raises AttributeError
instead of silently succeeding as a no-op. `atomicwrites` (which backs HA's
`Store`, and so the setpoint-memo tests) is unaffected either way — it gates
every fcntl path behind `sys.platform != 'win32'`.
"""

# Values from the Linux headers. Nothing reads them, but keeping them accurate
# means a stray comparison won't quietly do the wrong thing.
LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd, operation):
    """No-op: file locking is not available on Windows through this API."""
    return None
