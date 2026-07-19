from __future__ import annotations

import socket
import threading

from src.product_components.diagnostics.ibkr_connectivity import tcp_probe


def test_tcp_probe_reports_open_socket() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()

    def accept_once() -> None:
        conn, _ = server.accept()
        conn.close()
        server.close()

    thread = threading.Thread(target=accept_once)
    thread.start()

    result = tcp_probe(host, port, timeout_seconds=1)

    thread.join(timeout=2)
    assert result.ok is True
    assert result.error is None


def test_tcp_probe_reports_closed_socket() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    host, port = probe.getsockname()
    probe.close()

    result = tcp_probe(host, port, timeout_seconds=1)

    assert result.ok is False
    assert result.error
