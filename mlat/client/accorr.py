# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-client - an ADS-B multilateration client.
# Mode A/C burst / signal-strength correlation for the modeac.net fork.
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
Links Mode A and Mode C codes of one aircraft statistically, from ONE receiver's replies:
which codes are heard in the same beam dwells, at the same signal level, again and again.

Each time a rotating radar's beam passes an aircraft, this receiver hears a short burst of
that aircraft's squawk AND altitude replies (tens of ms), at the same signal level. So:

  1. per code, group replies into bursts (replies less than gap_ms apart) and keep each
     burst's time span and mean signal level (dBFS);
  2. when a burst ends, every other code with a burst overlapping it in time and a mean signal
     within signal_tol_db counts one co-occurrence for that code pair;
  3. over window_s, a pair is linked when it co-occurred in at least min_bursts bursts AND in
     at least min_ratio (0.4) of the bursts of the less frequent code (low because altitude codes
     are often shared by several aircraft at one flight level).

Two different aircraft only share a burst when they are in line from that radar at a similar
signal level; the next radar sees them at different bearings, so their ratio stays low. The
two codes of one aircraft co-occur in nearly every burst from every radar. This does not
depend on the radar's interrogation spacing (staggered / jittered PRI is fine), unlike the
sandwich test in acpair.py. It needs several dwells, so it links more slowly (tens of s).

Compare PlanePlotter's "ping-rate correlation" between Mode A and Mode C pings (6.5.1.2 /
6.5.2.3); this adds the per-burst signal level as a second, independent clue.
"""

import collections
import math

CODE_MASK = 0x7777
TICKS_PER_MS = 12000.0


def signal_db(byte):
    return 20.0 * math.log10(max(int(byte), 1) / 255.0)


class _Burst:
    __slots__ = ("code", "start", "end", "sum_db", "n")

    def __init__(self, code, ticks, db):
        self.code, self.start, self.end, self.sum_db, self.n = code, ticks, ticks, db, 1

    @property
    def mean_db(self):
        return self.sum_db / self.n


class BurstCorrelator:
    def __init__(self):
        self.open = collections.OrderedDict()     # code -> open _Burst, least recently updated first
        self.closed = collections.deque()         # recently closed bursts (for overlap checks)
        self.bursts = {}                          # code -> deque of burst end times (now) within window
        self.co = {}                              # (lo, hi) -> deque of co-occurrence times (now)
        self.by_code = {}                         # code -> set of keys in self.co
        self.last_ticks = None
        self.burst_count = 0
        self.configure({})

    def configure(self, cfg):
        cfg = cfg or {}
        gap = float(cfg.get("burst_gap_ms", 25))
        sig = float(cfg.get("burst_signal_tol_db", 2.0))
        min_bursts = int(cfg.get("min_bursts", 4))
        ratio = float(cfg.get("min_ratio", 0.4))
        window = float(cfg.get("corr_window_s", 120))
        min_n = int(cfg.get("min_replies_per_burst", 2))
        if not (1 <= gap <= 1000 and sig >= 0 and min_bursts >= 1 and 0 < ratio <= 1
                and 0 < window <= 3600 and min_n >= 1):
            raise ValueError("reply_pairing correlation: bad burst_gap_ms / burst_signal_tol_db / "
                             "min_bursts / min_ratio / corr_window_s / min_replies_per_burst")
        self.gap = gap * TICKS_PER_MS
        self.signal_tol = sig
        self.min_bursts = min_bursts
        self.min_ratio = ratio
        self.window = window
        self.min_n = min_n

    # ------------------------------------------------------------------ input
    def add(self, ticks, word, signal, now):
        if self.last_ticks is not None and ticks < self.last_ticks:
            self.open.clear()                     # clock reset: drop partial bursts
            self.closed.clear()
        self.last_ticks = ticks
        code = word & CODE_MASK
        db = signal_db(signal)

        # close bursts that have gone quiet (oldest-updated first)
        while self.open:
            c, b = next(iter(self.open.items()))
            if ticks - b.end <= self.gap:
                break
            del self.open[c]
            self._closed(b, now)

        b = self.open.get(code)
        if b is None:
            self.open[code] = _Burst(code, ticks, db)
        else:
            b.end = ticks
            b.sum_db += db
            b.n += 1
            self.open.move_to_end(code)

    def _closed(self, b, now):
        if b.n < self.min_n:
            return                                # a lone reply is not a dwell (could be FRUIT)
        self.burst_count += 1
        q = self.bursts.get(b.code)
        if q is None:
            q = self.bursts[b.code] = collections.deque()
        q.append(now)
        mean = b.mean_db
        # co-occurrence with bursts that overlap in time and closed before this one: each overlapping
        # pair is counted exactly once, when the second of the two closes
        horizon = b.start - self.gap
        while self.closed and self.closed[0].end < horizon - 4 * self.gap:
            self.closed.popleft()
        partners = set()
        for o in self.closed:
            if o.code != b.code and o.end >= horizon and o.start <= b.end + self.gap \
                    and abs(o.mean_db - mean) <= self.signal_tol:
                partners.add(o.code)
        for c in partners:
            key = (c, b.code) if c < b.code else (b.code, c)
            q = self.co.get(key)
            if q is None:
                q = self.co[key] = collections.deque()
                self.by_code.setdefault(key[0], set()).add(key)
                self.by_code.setdefault(key[1], set()).add(key)
            q.append(now)
        self.closed.append(b)

    # ----------------------------------------------------------------- output
    def _count(self, q, now):
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q)

    def _score(self, key, now):
        q = self.co.get(key)
        if not q:
            return 0, 0.0
        n_co = self._count(q, now)
        if not n_co:
            del self.co[key]
            for c in key:
                ks = self.by_code.get(c)
                if ks is not None:
                    ks.discard(key)
                    if not ks:
                        del self.by_code[c]
            return 0, 0.0
        na = self._count(self.bursts.get(key[0], collections.deque()), now)
        nb = self._count(self.bursts.get(key[1], collections.deque()), now)
        ratio = min(1.0, n_co / max(1, min(na, nb)))
        return n_co, ratio

    def _linked(self, key, now):
        n_co, ratio = self._score(key, now)
        return n_co >= self.min_bursts and ratio >= self.min_ratio

    def partners(self, code, now):
        code &= CODE_MASK
        return [k[1] if k[0] == code else k[0] for k in list(self.by_code.get(code, ()))
                if self._linked(k, now)]

    def links(self, now):
        out = []
        for key in list(self.co):
            n_co, ratio = self._score(key, now)
            if n_co >= self.min_bursts and ratio >= self.min_ratio:
                out.append((key[0], key[1], n_co))
        return out
