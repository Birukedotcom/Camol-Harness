"""Owned, abortable local-chat HTTP transport; no redirects or proxy discovery."""

import http.client
import os
import socket
import threading
from urllib.parse import urlsplit

from .connections import validate_loopback_endpoint


class AbortableLocalHTTP:
    def __init__(self):
        self._lock = threading.Lock()
        self._interrupter = None
        self._aborted = False

    def abort(self):
        with self._lock:
            self._aborted = True
            if self._interrupter is not None:
                try:
                    self._interrupter.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def request(self, endpoint, payload, timeout):
        endpoint = validate_loopback_endpoint(endpoint)
        target = urlsplit(endpoint)
        factory = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        # Numeric loopback has no DNS phase. Connection/TLS setup is individually
        # bounded while no connected socket yet exists to interrupt.
        connection = factory(target.hostname, target.port, timeout=min(timeout, 1.0))
        response = None
        interrupter = None
        try:
            with self._lock:
                if self._aborted:
                    raise OSError("local request was aborted")
            connection.connect()
            # Retain a dedicated socket descriptor solely for shutdown. This
            # works for both HTTP and TLS without reaching through HTTPResponse
            # private buffers, even when Connection:close detaches conn.sock.
            interrupter = socket.socket(fileno=os.dup(connection.sock.fileno()))
            with self._lock:
                if self._aborted:
                    raise OSError("local request was aborted")
                self._interrupter = interrupter
            connection.sock.settimeout(timeout)
            path = (target.path.rstrip("/") if target.path else "") + "/chat/completions"
            connection.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise OSError("local endpoint returned a non-success status")
            value = response.read((8 << 20) + 1)
            if len(value) > 8 << 20:
                raise OSError("local endpoint exceeded the bounded response size")
            return value
        finally:
            # abort() only shuts down a socket; the request thread owns closing
            # buffered response objects, avoiding cross-thread read-lock hangs.
            if response is not None:
                response.close()
            connection.close()
            with self._lock:
                self._interrupter = None
                if interrupter is not None:
                    interrupter.close()
