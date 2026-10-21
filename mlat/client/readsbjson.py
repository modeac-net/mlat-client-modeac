# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-client - an ADS-B multilateration client.
# modeac.net: known squawks from the local readsb's aircraft list.
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
Reads the squawks readsb itself has decoded, from its aircraft.json on this machine (ultrafeeder
serves it at http://<host>/data/aircraft.json). readsb takes squawks from every source it handles -
Mode S identity replies, ADS-B status, ADS-R, UAT from dump978 - so the pre-filter's Mode A match
knows everything this receiver knows, without the client decoding anything new.

Only local: nothing is downloaded from the network. Polled in a background thread so a slow or
missing web server never stalls the client; if it can't be read, the client simply works from its
own identity replies, as before.
"""

import json
import threading
import time
import urllib.request

from mlat.client.util import log

# entries that are not a cooperative aircraft: TIS-B is the FAA rebroadcasting radar tracks, which
# include Mode A/C-only aircraft (possibly our target), so their squawk must never be "known"
IGNORE_TYPES = frozenset(('tisb_icao', 'tisb_trackfile', 'tisb_other', 'mode_ac'))
MAX_SEEN_S = 60.0


def known_squawks(doc):
    """Hex-octal squawk codes of the aircraft in a readsb aircraft.json document."""
    out = set()
    for a in doc.get('aircraft', []):
        if a.get('type') in IGNORE_TYPES or str(a.get('hex', '')).startswith('~'):
            continue          # non-ICAO entries are TIS-B / anonymous: not evidence of a transponder we hear
        sq = a.get('squawk')
        seen = a.get('seen', 0)
        if not sq or not isinstance(seen, (int, float)) or seen > MAX_SEEN_S:
            continue
        sq = str(sq)
        if len(sq) == 4 and all(c in '01234567' for c in sq):
            out.add(int(sq, 16))
    return out


class LocalAircraftList:
    def __init__(self, url, interval=10.0):
        self.url = url
        self.interval = interval
        self.latest = None            # (fetched_at, set of codes), replaced atomically by the thread
        self.taken = None
        self.ok = None
        self.thread = threading.Thread(target=self._run, name='readsb-aircraft-json', daemon=True)
        self.thread.start()

    def _run(self):
        failing_since = None
        while True:
            try:
                req = urllib.request.Request(self.url, headers={'User-Agent': 'mlat-client-modeac'})
                with urllib.request.urlopen(req, timeout=5) as r:
                    doc = json.load(r)
                self.latest = (time.monotonic(), known_squawks(doc))
                if self.ok is not True:
                    log('Mode A/C: reading known squawks from {0}', self.url)
                self.ok, failing_since = True, None
            except Exception as e:                     # noqa: BLE001 - optional source, keep going
                if self.ok is not False:
                    log('Mode A/C: cannot read {0} ({1}); using this receiver\'s identity replies only',
                        self.url, e)
                self.ok = False
                failing_since = failing_since or time.monotonic()
            # back off while it keeps failing (e.g. readsb without a web server)
            time.sleep(self.interval if self.ok else min(300.0, self.interval * 12))

    def take(self):
        """The newest set of codes, once per fetch; None if nothing new."""
        snap = self.latest
        if snap is None or snap is self.taken:
            return None
        self.taken = snap
        return snap[1]
