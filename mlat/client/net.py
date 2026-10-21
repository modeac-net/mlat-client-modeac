# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-client - an ADS-B multilateration client.
# Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Common networking bits, based on asyncore
"""

import sys
import socket
import asyncore
from mlat.client.util import log, log_exc, monotonic_time

import random
random.seed()

__all__ = ('LoggingMixin', 'ReconnectingConnection')


class LoggingMixin:
    """A mixin that redirects asyncore's logging to the client's
    global logging."""

    def log(self, message):
        log('{0}', message)

    def log_info(self, message, type='info'):
        log('{0}: {1}', message, type)


class ReconnectingConnection(LoggingMixin, asyncore.dispatcher):
    """
    An asyncore connection that maintains a TCP connection to a particular
    host/port, reconnecting on connection loss.
    """

    reconnect_interval = 10.0
    # modeac.net: back off when connections keep failing (server down, refused, unreachable),
    # so a whole network of clients doesn't hammer the server or relay every 10 s.
    # 0 = off (the local receiver connection keeps retrying quickly).
    max_backoff = 0.0

    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.host = host
        self.port = port
        self.addrlist = []
        self.state = 'disconnected'
        self.reconnect_at = None
        self.last_try = 0

        self.failures = 0
        self.fail_streak = 0          # consecutive short-lived connections / failed attempts
        self._suppress_errors = 0
        self.suppress_until = 0
        self.motdShown = 0

    @property
    def suppress_errors(self):
        mono = monotonic_time()
        if self._suppress_errors and mono > self.suppress_until:
            # reset error suppression
            self.failures = 2
            self._suppress_errors = 0
            self.suppress_until = 0

        return self._suppress_errors

    def set_error_suppression(self):
        self._suppress_errors = 1
        self.suppress_until = monotonic_time() + 900
        log('Connection retries will continue, further messages about this connection will be suppressed for 15 minutes')

    def heartbeat(self, now):
        if self.reconnect_at is None or self.reconnect_at > now:
            return
        if self.state == 'ready':
            return
        self.reconnect_at = None
        self.reconnect()

    def close(self, manual_close=False):
        mono = monotonic_time()
        if mono - self.last_try < 5 * 60:
            # connections shorter than 5 minutes count as failures
            self.failures += 1
            self.fail_streak += 1
            #log(f"failures {self.failures}")
        else:
            self.fail_streak = 0

        if self.failures == 3:
            self.set_error_suppression()

        try:
            asyncore.dispatcher.close(self)
        except AttributeError:
            # blarg, try to eat asyncore bugs
            pass

        if self.state != 'disconnected':
            if not manual_close and not self.suppress_errors:
                log('Lost connection to {host}:{port}', host=self.host, port=self.port)

            self.state = 'disconnected'
            self.reset_connection()
            self.lost_connection()

        if not manual_close:
            self.schedule_reconnect()

    def disconnect(self, reason):
        if self.state != 'disconnected':
            if not self.suppress_errors:
                log('Disconnecting from {host}:{port}: {reason}', host=self.host, port=self.port, reason=reason)
            self.close(True)

    def writable(self):
        return self.connecting

    def schedule_reconnect(self):
        if self.reconnect_at is None:
            mono = monotonic_time()
            other_addresses = 0

            if len(self.addrlist) > 0:
                # we still have more addresses to try
                # nb: asyncore breaks in odd ways if you try
                # to reconnect immediately at this point
                # (pending events for the old socket go to
                # the new socket) so do it in 0.5s time
                # so the caller can clean up the old
                # socket and discard the events.
                interval = 0.5
                other_addresses = 1
            else:

                interval = self.last_try + self.retry_delay() - mono + 2 * random.random()

                if interval < 4:
                    interval = 2 + 2 * random.random()

            if not self.suppress_errors and not other_addresses:
                #log(f'Reconnecting in {interval:.1f} seconds')
                pass

            self.reconnect_at = mono + interval

    def retry_delay(self):
        """Seconds between connection attempts: the server's requested interval, stretched
        while attempts keep failing (10, 20, 40 ... s up to max_backoff, with jitter so
        clients don't all return at once after an outage)."""
        delay = self.reconnect_interval
        if self.max_backoff > 0 and self.fail_streak >= 2:
            backoff = min(self.max_backoff, 10.0 * 2 ** (self.fail_streak - 1))
            delay = max(delay, backoff * random.uniform(0.75, 1.0))
        return delay

    def refresh_address_list(self):
        self.address

    def reconnect(self):
        if self.state != 'disconnected':
            self.disconnect('About to reconnect')

        mono = monotonic_time()

        self.last_try = mono
        try:
            self.reset_connection()


            if len(self.addrlist) == 0:
                # ran out of addresses to try, resolve it again

                self.addrlist = socket.getaddrinfo(host=self.host,
                                                   port=self.port,
                                                   family=socket.AF_UNSPEC,
                                                   type=socket.SOCK_STREAM,
                                                   proto=0,
                                                   flags=0)

            # try the next available address
            a_family, a_type, a_proto, a_canonname, a_sockaddr = self.addrlist[0]
            del self.addrlist[0]

            self.create_socket(a_family, a_type)
            self.connect(a_sockaddr)
        except socket.error as e:
            if not self.suppress_errors:
                log('Connection to {host}:{port} failed: {ex!s}', host=self.host, port=self.port, ex=e)
            self.close()

    def handle_connect(self):
        self.state = 'connected'
        self.addrlist = []  # connect was OK, re-resolve next time
        self.start_connection()

    def handle_read(self):
        pass

    def handle_write(self):
        pass

    def handle_close(self):
        self.close()

    def handle_error(self):
        t, v, tb = sys.exc_info()
        if isinstance(v, IOError):
            if not self.suppress_errors:
                log('Connection to {host}:{port} lost: {ex!s}',
                    host=self.host,
                    port=self.port,
                    ex=v)
        else:
            log_exc('Unexpected exception on connection to {host}:{port}',
                    host=self.host,
                    port=self.port)

        self.handle_close()

    def reset_connection(self):
        pass

    def start_connection(self):
        pass

    def lost_connection(self):
        pass
