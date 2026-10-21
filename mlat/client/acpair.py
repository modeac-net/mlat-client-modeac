# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-client - an ADS-B multilateration client.
# Mode A/C reply-train pairing for the modeac.net fork.
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
Pairs Mode A and Mode C codes of the same aircraft from ONE receiver's reply timing.

A secondary radar's beam dwells on an aircraft for tens of milliseconds and interrogates it
every few milliseconds, interlacing Mode A and Mode C (A C A C..., A A C A A C..., ...). The
aircraft answers every interrogation, so this receiver hears a train

    X  Y  X  Y  X ...      (X = squawk, Y = altitude code)

with the radar's interrogation spacing between replies (steady to nanoseconds: the aircraft
moves well under a metre between replies) and nearly the same signal level. A reply Y with
an X reply on each side, both a plausible interrogation interval away and matching signal
level, is a "sandwich" - evidence that X and Y are one aircraft - when either

  - the two gaps are equal within spacing_tol_us (constant-PRI radars, any interlace), or
  - (stagger_test, off by default) the next Y follows exactly one X-to-X interval later
    (X Y X Y on radars whose PRI alternates between two values). This test cannot see an
    offset between two aircraft (gaps a+d, b-d still sum to a+b), so in a crowded beam it
    links neighbours; it needs the radar's actual PRI pattern to be safe.

The tolerance is a few microseconds: another aircraft in the same beam whose replies land
within it would be within ~1-2 km of the same path length, and mostly garbled anyway.

Codes are linked after `min_hits` sandwiches within `window_s` seconds. A false sandwich
needs another aircraft's reply to land at the right time AND signal level; real pairs give
several sandwiches per beam dwell, sweep after sweep.

Timestamps are this receiver's 12 MHz clock, so no clock sync is involved.
"""

import collections
import math

CODE_MASK = 0x7777
TICKS_PER_US = 12.0


def signal_db(byte):
    """Beast signal byte (amplitude scale, 255 = full scale) -> dBFS."""
    return 20.0 * math.log10(max(int(byte), 1) / 255.0)


class TrainPairer:
    def __init__(self):
        self.recent = collections.deque()        # (ticks, code, sig_db), time order
        self.pending = collections.deque()       # replies whose right-hand side isn't complete yet
        self.hits = {}                           # (lo, hi) code pair -> deque of sandwich times (now)
        self.by_code = {}                        # code -> set of keys in self.hits containing it
        self.sandwiches = 0
        self.gap_hist = None                     # optional collections.Counter of sandwich gaps (10 us bins)
        self.configure({})

    def configure(self, cfg):
        cfg = cfg or {}
        self.min_gap = float(cfg.get("min_gap_us", 1500)) * TICKS_PER_US
        self.max_gap = float(cfg.get("max_gap_us", 6000)) * TICKS_PER_US
        self.spacing_tol = float(cfg.get("spacing_tol_us", 5)) * TICKS_PER_US
        self.stagger_test = bool(cfg.get("stagger_test", False))
        self.signal_tol = float(cfg.get("signal_tol_db", 3.0))
        self.min_hits = int(cfg.get("min_hits", 3))
        self.window = float(cfg.get("window_s", 30))
        if not (0 < self.min_gap < self.max_gap <= 20000 * TICKS_PER_US):
            raise ValueError("reply_pairing: need 0 < min_gap_us < max_gap_us <= 20000")
        if not (self.min_hits >= 1 and 0 < self.window <= 3600 and self.signal_tol >= 0):
            raise ValueError("reply_pairing: bad min_hits / window_s / signal_tol_db")

    # ------------------------------------------------------------------ input
    def add(self, ticks, word, signal, now):
        """One Mode A/C reply from this receiver (12 MHz ticks, A/C word, Beast signal byte)."""
        rec = (ticks, word & CODE_MASK, signal_db(signal))
        if self.recent and ticks < self.recent[-1][0]:
            # out of order / clock reset: start over rather than mis-pair
            self.recent.clear()
            self.pending.clear()
        self.recent.append(rec)
        self.pending.append(rec)

        # a pending reply can be judged once nothing more can arrive inside its right-hand window
        # (two gaps: the staggered-PRI test looks for the next Y)
        while self.pending and self.pending[0][0] + 2 * self.max_gap < ticks:
            self._judge(self.pending.popleft(), now)
        # nothing older than three windows is ever needed again
        horizon = ticks - 3 * self.max_gap
        while self.recent and self.recent[0][0] < horizon:
            self.recent.popleft()

    def _judge(self, mid, now):
        tm, ym, sm = mid
        left, right, again = {}, {}, []
        for t, c, s in self.recent:
            if abs(s - sm) > self.signal_tol:
                continue
            dt = t - tm
            if c == ym:
                if 2 * self.min_gap <= dt <= 2 * self.max_gap:
                    again.append(dt)
                continue
            if -self.max_gap <= dt <= -self.min_gap:
                left.setdefault(c, []).append(-dt)
            elif self.min_gap <= dt <= self.max_gap:
                right.setdefault(c, []).append(dt)
        for c in left.keys() & right.keys():
            gap = self._spaced(left[c], right[c], again)
            if gap:
                self.sandwiches += 1
                if self.gap_hist is not None:
                    self.gap_hist[int(round(gap / TICKS_PER_US / 10.0)) * 10] += 1
                key = (c, ym) if c < ym else (ym, c)
                q = self.hits.get(key)
                if q is None:
                    q = self.hits[key] = collections.deque()
                    self.by_code.setdefault(key[0], set()).add(key)
                    self.by_code.setdefault(key[1], set()).add(key)
                q.append(now)

    def _spaced(self, gaps1, gaps2, again):
        """Mean gap (ticks) of the first left/right pair passing either spacing test, or 0."""
        tol = self.spacing_tol
        for g1 in gaps1:
            for g2 in gaps2:
                if abs(g1 - g2) <= tol:
                    return 0.5 * (g1 + g2)                     # equal gaps
                for d in (again if self.stagger_test else ()):
                    if abs(d - (g1 + g2)) <= tol:
                        return 0.5 * (g1 + g2)                 # X Y X Y, staggered PRI
        return 0

    # ----------------------------------------------------------------- output
    def _count(self, key, now):
        q = self.hits.get(key)
        if not q:
            return 0
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            del self.hits[key]
            for c in key:
                ks = self.by_code.get(c)
                if ks is not None:
                    ks.discard(key)
                    if not ks:
                        del self.by_code[c]
            return 0
        return len(q)

    def partners(self, code, now):
        """Codes currently linked with `code`."""
        code &= CODE_MASK
        out = []
        for key in list(self.by_code.get(code, ())):
            if self._count(key, now) >= self.min_hits:
                out.append(key[1] if key[0] == code else key[0])
        return out

    def links(self, now):
        """[(code_a, code_b, hits)] for every linked pair."""
        out = []
        for key in list(self.hits):
            n = self._count(key, now)
            if n >= self.min_hits:
                out.append((key[0], key[1], n))
        return out
