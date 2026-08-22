import pytest
import pytest_socket

# pytest-homeassistant-custom-component unconditionally calls
# pytest_socket.disable_socket() before every test. On Windows, asyncio's
# event loop needs a real socketpair for its internal self-pipe, so blocking
# all sockets breaks fixture setup before any test code runs. We don't make
# real network calls in these tests (the JNAP client is always mocked), so
# disable that safety net rather than fight the event loop over it.
pytest_socket.disable_socket = lambda *args, **kwargs: None

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
