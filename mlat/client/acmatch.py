# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-client - an ADS-B multilateration client.
# Mode A/C pre-filter for the modeac.net fork.
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
Drops Mode A/C replies this receiver can already explain, before they are sent.

Same idea as readsb's trackMatchAC(): a Mode A/C code that equals the squawk (or
Mode C altitude) of a Mode S aircraft this receiver is hearing almost certainly
comes from that aircraft. readsb only uses its match for the console display;
here it saves the upload and the server's solve.

Four tiers, each switched on/off by the SERVER (handshake response and later
"modeac_filter" messages), so volunteers never need to touch anything:

  exclude      codes always dropped (default: 1200)
  mode_a_match drop a code that is the squawk of a Mode S aircraft heard in the
               last window_s seconds (squawk from DF5/DF21 identity replies, only
               for addresses already confirmed by DF11/DF17). Codes in
               never_drop (4000, 7000, emergencies...) are never dropped this way:
               a Mode S-off jet on 4000 next to a tanker on 4000 must get through.
  mode_c_match drop a code that is the Mode C altitude (+/- tolerance_ft) of a
               Mode S aircraft heard in the last window_s seconds. Off by default:
               airliners fill most flight levels, so this also eats unknowns'
               altitude replies.
  reply_pairing pair Mode A and Mode C codes of one aircraft from this receiver's reply
               timing ("sandwich": X Y X at the radar's interrogation spacing and the
               same signal level, see acpair.py). drop_paired: drop a code whose every
               linked partner is dropped by exclude / mode_a_match - i.e. the altitude
               replies of known Mode S traffic and of 1200 VFR, but not of an unknown on
               4000. send_hints: report links between kept codes (an unknown's squawk and
               altitude) to the server so its altitude can be shown at once.
               sandwich (on) / correlation (off): which linkers run. correlation =
               codes heard in the same beam dwells at the same mean signal level, dwell
               after dwell (accorr.py); slower but independent of radar timing.

With no configuration from the server, nothing is filtered.
"""

from mlat.client.acpair import TrainPairer
from mlat.client.accorr import BurstCorrelator

CODE_MASK = 0x7777

_DEFAULT_NEVER_DROP = ("0000", "1200", "1255", "1277", "4000", "7000", "7400", "7500", "7600", "7700", "7777")


def code_from_str(s):
    """'4000' -> 0x4000 (readsb's hex-octal word)."""
    s = str(s).strip()
    if len(s) != 4 or any(c not in "01234567" for c in s):
        raise ValueError("not a 4-digit octal squawk: %r" % s)
    return int(s, 16)


def code_to_str(code):
    return "%04x" % (code & CODE_MASK)


def id13_to_code(id13):
    """13-bit Mode S identity field (DF5/DF21) -> hex-octal Mode A word.
    Bit order C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4 (port of readsb's decodeID13Field)."""
    code = 0
    for bit, mask in ((0x1000, 0x0010), (0x0800, 0x1000), (0x0400, 0x0020), (0x0200, 0x2000),
                      (0x0100, 0x0040), (0x0080, 0x4000),
                      (0x0020, 0x0100), (0x0010, 0x0001), (0x0008, 0x0200), (0x0004, 0x0002),
                      (0x0002, 0x0400), (0x0001, 0x0004)):
        if id13 & bit:
            code |= mask
    return code


def squawk_from_frame(data):
    """Mode A code from a DF5/DF21 frame (bytes), or None."""
    if len(data) < 4:
        return None
    df = data[0] >> 3
    if df not in (5, 21):
        return None
    return id13_to_code(((data[2] << 8) | data[3]) & 0x1FFF)


def mode_c_altitude(code):
    """Gillham decode of a hex-octal word -> feet, or None (port of readsb's internalModeAToModeC)."""
    if code & 0x8889 or not code & 0x0070:
        return None
    c = 0
    if code & 0x0010:
        c ^= 7
    if code & 0x0020:
        c ^= 3
    if code & 0x0040:
        c ^= 1
    if c & 5 == 5:
        c ^= 2
    if c > 5:
        return None
    n = 0
    for mask, flip in ((0x0002, 0xFF), (0x0004, 0x7F), (0x1000, 0x3F), (0x2000, 0x1F),
                       (0x4000, 0x0F), (0x0100, 0x07), (0x0200, 0x03), (0x0400, 0x01)):
        if code & mask:
            n ^= flip
    if n & 1:
        c = 6 - c
    return (5 * n + c - 13) * 100


_ALT_TO_CODE = {}
for _code in range(0x8000):
    if not _code & 0x8888:
        _a = mode_c_altitude(_code)
        if _a is not None:
            _ALT_TO_CODE[_a] = _code
del _code, _a
_ALT_CODES = frozenset(_ALT_TO_CODE.values())        # every code that can be a Mode C reply


def altitude_codes(alt_ft, tolerance_ft):
    """Mode C codes within tolerance of an altitude (100 ft steps, like readsb's +/-1 step)."""
    centre = int((alt_ft + 49) // 100)
    steps = max(0, int(tolerance_ft // 100))
    out = []
    for k in range(centre - steps, centre + steps + 1):
        code = _ALT_TO_CODE.get(k * 100)
        if code is not None:
            out.append(code)
    return out


def _tier(cfg, name):
    t = cfg.get(name) if isinstance(cfg, dict) else None
    return t if isinstance(t, dict) else {}


class ModeACMatcher:
    def __init__(self):
        self.squawks = {}       # code -> last time a Mode S aircraft was heard with this squawk
        self.altitudes = {}     # code -> last time a Mode S aircraft was heard at this altitude
        self.pairer = TrainPairer()
        self.correlator = BurstCorrelator()
        self._links, self._adjacent, self._links_at = {}, {}, None
        self.configure(None)

    # ------------------------------------------------------------ settings
    def configure(self, cfg):
        """Apply the server's settings. None / {} = filter nothing. Raises ValueError on bad input
        (the previous settings are kept)."""
        ex = _tier(cfg, "exclude")
        ma = _tier(cfg, "mode_a_match")
        mc = _tier(cfg, "mode_c_match")
        exclude_on = bool(ex.get("enabled", False))
        exclude = frozenset(code_from_str(c) for c in ex.get("codes", ()))
        mode_a_on = bool(ma.get("enabled", False))
        mode_a_window = float(ma.get("window_s", 60))
        never_drop = frozenset(code_from_str(c) for c in ma.get("never_drop", _DEFAULT_NEVER_DROP))
        mode_c_on = bool(mc.get("enabled", False))
        mode_c_window = float(mc.get("window_s", 30))
        mode_c_tol = float(mc.get("tolerance_ft", 100))
        if not (0 < mode_a_window <= 3600 and 0 < mode_c_window <= 3600 and 0 <= mode_c_tol <= 1000):
            raise ValueError("modeac_filter: window_s must be 0..3600 and tolerance_ft 0..1000")
        rp = _tier(cfg, "reply_pairing")
        pair_on = bool(rp.get("enabled", False))
        pair_drop = bool(rp.get("drop_paired", False))
        pair_hints = bool(rp.get("send_hints", True))
        hint_interval = float(rp.get("hint_interval_s", 5))
        if not 1 <= hint_interval <= 3600:
            raise ValueError("reply_pairing: hint_interval_s must be 1..3600")
        use_sandwich = bool(rp.get("sandwich", True))
        use_corr = bool(rp.get("correlation", False))
        TrainPairer().configure(rp)          # validate before changing anything
        BurstCorrelator().configure(rp)

        self.exclude_on, self.exclude = exclude_on, exclude
        self.mode_a_on, self.mode_a_window, self.never_drop = mode_a_on, mode_a_window, never_drop
        self.mode_c_on, self.mode_c_window, self.mode_c_tol = mode_c_on, mode_c_window, mode_c_tol
        self.pair_on, self.pair_drop, self.pair_hints = pair_on, pair_drop, pair_hints
        self.hint_interval = hint_interval
        self.use_sandwich, self.use_corr = use_sandwich, use_corr
        self.pairer.configure(rp)
        self.correlator.configure(rp)
        self._links_at = None
        if not mode_c_on:
            self.altitudes.clear()
        if not mode_a_on:
            self.squawks.clear()

    def describe(self):
        parts = []
        parts.append("exclude " + (",".join(sorted(code_to_str(c) for c in self.exclude)) or "nothing")
                     if self.exclude_on else "exclude off")
        parts.append("Mode A match on (%.0f s)" % self.mode_a_window if self.mode_a_on else "Mode A match off")
        parts.append("Mode C match on (%.0f s, +/-%.0f ft)" % (self.mode_c_window, self.mode_c_tol)
                     if self.mode_c_on else "Mode C match off")
        if self.pair_on:
            parts.append("reply pairing on (%s; drop paired %s, hints %s)" % (
                "+".join(m for m, on in (("sandwich", self.use_sandwich), ("correlation", self.use_corr)) if on)
                or "no method",
                "on" if self.pair_drop else "off", "on" if self.pair_hints else "off"))
        else:
            parts.append("reply pairing off")
        return "; ".join(parts)

    def observe(self, ticks, word, signal, now):
        """Every Mode A/C reply, before any filtering: feeds the reply-train pairer."""
        if self.pair_on:
            if self.use_sandwich:
                self.pairer.add(ticks, word, signal, now)
            if self.use_corr:
                self.correlator.add(ticks, word, signal, now)

    def partners(self, code, now):
        """Codes linked with `code` (see links())."""
        self._refresh(now)
        return self._adjacent.get(code & CODE_MASK, ())

    def links(self, now):
        """{(lo, hi): evidence} from every enabled method, after two rules about real aircraft:
          - an aircraft has one squawk: two codes that both cannot be Mode C altitudes never link
          - a discrete squawk (not a shared code like 4000) is one aircraft, so it has one altitude:
            keep only its strongest altitude partner, plus partners within 300 ft of that one
            (a climbing aircraft's previous / next altitude code)."""
        self._refresh(now)
        return dict(self._links)

    def _refresh(self, now):
        if self._links_at is not None and 0 <= now - self._links_at < 1.0:
            return
        raw = {}
        for on, linker in ((self.use_sandwich, self.pairer), (self.use_corr, self.correlator)):
            if on:
                for a, b, n in linker.links(now):
                    if a in _ALT_CODES or b in _ALT_CODES:
                        key = (a, b) if a < b else (b, a)
                        raw[key] = max(n, raw.get(key, 0))
        by_squawk = {}
        for (a, b), n in raw.items():
            for sq, alt in ((a, b), (b, a)):
                if sq not in _ALT_CODES and sq not in self.never_drop:
                    by_squawk.setdefault(sq, []).append((n, alt))
        for sq, cands in by_squawk.items():
            if len(cands) < 2:
                continue
            best_alt = mode_c_altitude(max(cands)[1])
            for n, alt in cands:
                if abs(mode_c_altitude(alt) - best_alt) > 300:
                    raw.pop((min(sq, alt), max(sq, alt)), None)
        adjacent = {}
        for a, b in raw:
            adjacent.setdefault(a, set()).add(b)
            adjacent.setdefault(b, set()).add(a)
        self._links, self._adjacent, self._links_at = raw, adjacent, now

    # ------------------------------------------------------- Mode S input
    def note_squawk(self, code, now):
        self.squawks[code & CODE_MASK] = now

    def note_altitude(self, alt_ft, now):
        for code in altitude_codes(alt_ft, self.mode_c_tol):
            self.altitudes[code] = now

    # --------------------------------------------------------- the check
    def _drops(self, code, now):
        """Would exclude / mode_a_match drop this code? ('exclude', 'mode_a' or None)"""
        if self.exclude_on and code in self.exclude:
            return "exclude"
        if self.mode_a_on and code not in self.never_drop:
            t = self.squawks.get(code)
            if t is not None and now - t <= self.mode_a_window:
                return "mode_a"
        return None

    def check(self, word, now):
        """None to send this Mode A/C reply, else the tier that drops it:
        'exclude', 'mode_a', 'mode_c' or 'pair'."""
        code = word & CODE_MASK
        reason = self._drops(code, now)
        if reason is not None:
            return reason
        if self.mode_c_on:
            t = self.altitudes.get(code)
            if t is not None and now - t <= self.mode_c_window:
                return "mode_c"
        if self.pair_on and self.pair_drop:
            partners = self.partners(code, now)
            if partners and all(self._drops(p, now) for p in partners):
                return "pair"
        return None

    def hints(self, now):
        """Linked code pairs where both codes are kept: [['4000', '2615', hits], ...]"""
        if not (self.pair_on and self.pair_hints):
            return []
        return [[code_to_str(a), code_to_str(b), n] for (a, b), n in self.links(now).items()
                if self._drops(a, now) is None and self._drops(b, now) is None]
