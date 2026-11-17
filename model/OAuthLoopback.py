"""Bound the lifetime of an accepted OAuth loopback connection, not just accept."""
from http.server import HTTPServer
import socket
import threading


class OAuthLoopbackServer(HTTPServer):
    # HTTPServer.timeout applies to accept only. A peer that sends no headers
    # otherwise prevents handle_request() returning to the cancellation loop.
    connection_timeout = 1.0

    def get_request(self):
        connection, address = super().get_request()
        try:
            connection.settimeout(self.connection_timeout)
        except BaseException:
            connection.close()
            raise
        return connection, address

    def process_request(self, request, client_address):
        # An inactivity timeout alone does not stop a peer dripping one header
        # byte at a time. Bound the complete request so the authorization loop
        # always gets another opportunity to cancel or enforce its deadline.
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # The handler may already have closed this same socket.
        timer = threading.Timer(self.connection_timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            return super().process_request(request, client_address)
        finally:
            timer.cancel()
