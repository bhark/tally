from __future__ import annotations

import socket

from tally import probe


def test_tcp_open_false_on_closed_port():
    # bind a socket without listening so the port refuses connects
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert probe._tcp_open("127.0.0.1", port, timeout=1) is False
