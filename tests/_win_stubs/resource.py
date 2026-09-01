"""Minimal Windows stub for the POSIX-only stdlib `resource` module.

Imported at module scope by `homeassistant.util.resource`, which
`homeassistant.runner` imports, which the pytest plugin imports — same chain
as the `fcntl` stub in this directory.

The only caller is `set_open_file_descriptor_limit()`, which raises the soft
open-file limit to 2048 and returns early when the current soft limit already
meets it. Reporting Windows' actual CRT ceiling (8192 stdio streams, the max
`_setmaxstdio` accepts) makes it take that early return, so no limit is ever
faked as having been changed.
"""

RLIMIT_NOFILE = 7

# The Windows C runtime caps concurrently open stdio streams at 8192.
_WINDOWS_MAX_STDIO = 8192


def getrlimit(resource_id):
    """Report the Windows CRT open-file ceiling as both soft and hard limit."""
    return (_WINDOWS_MAX_STDIO, _WINDOWS_MAX_STDIO)


def setrlimit(resource_id, limits):
    """No-op: resource limits are not adjustable through this API on Windows."""
    return None
