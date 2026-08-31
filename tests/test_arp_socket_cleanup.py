"""Finding 08: the ARP-priming socket leaked on the error path.

_get_mac_with_arp_refresh() opens a UDP socket purely to force a packet onto
the wire so the ARP cache gets populated. The close() used to sit inside the
try block, so a raise from connect() or send() left the socket to the garbage
collector. It runs once per setup, so this never bit anyone - but the fix is
a context manager and the guarantee is worth pinning.
"""

from unittest.mock import MagicMock, patch

from custom_components.uponorx265.helper import _get_mac_with_arp_refresh

HOST = "10.0.0.1"


def _socket_raising_on(method):
    sock = MagicMock()
    sock.__enter__.return_value = sock
    getattr(sock, method).side_effect = OSError(f"{method} failed")
    return sock


def test_socket_closed_when_connect_raises():
    sock = _socket_raising_on("connect")
    with patch("custom_components.uponorx265.helper.socket.socket", return_value=sock), \
         patch("custom_components.uponorx265.helper.get_mac_address", return_value=None), \
         patch("custom_components.uponorx265.helper._get_mac_from_proc_arp", return_value=None):
        assert _get_mac_with_arp_refresh(HOST) is None

    sock.__exit__.assert_called_once()


def test_socket_closed_when_send_raises():
    sock = _socket_raising_on("send")
    with patch("custom_components.uponorx265.helper.socket.socket", return_value=sock), \
         patch("custom_components.uponorx265.helper.get_mac_address", return_value=None), \
         patch("custom_components.uponorx265.helper._get_mac_from_proc_arp", return_value=None):
        assert _get_mac_with_arp_refresh(HOST) is None

    sock.__exit__.assert_called_once()


def test_socket_closed_on_the_success_path():
    sock = MagicMock()
    sock.__enter__.return_value = sock
    with patch("custom_components.uponorx265.helper.socket.socket", return_value=sock), \
         patch("custom_components.uponorx265.helper.get_mac_address", return_value="aa:bb:cc:dd:ee:ff"), \
         patch("custom_components.uponorx265.helper.time.sleep"):
        assert _get_mac_with_arp_refresh(HOST) == "aa:bb:cc:dd:ee:ff"

    sock.__exit__.assert_called_once()
