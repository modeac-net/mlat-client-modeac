#!/usr/bin/env python3
"""
Offline tests for the Mode A/C forwarding fork.

Feeds hand-built Beast frames through the real C reader and the real
coordinator / UDP packer, with a fake server, and checks that:
  - Mode A/C frames come out of the reader (they used to be discarded)
  - their timestamps are shifted from F2 back to F1 (-244 ticks @ 12 MHz)
  - A/C frames lagging Mode S by < 1 s are kept, older ones dropped
  - excluded squawks (incl. with SPI set) are dropped, others forwarded
  - the UDP packet carries the new TYPE_MLAT_MODEAC record
  - the receiver asks readsb for Mode A/C in the Beast settings message
  - CLI squawk parsing rejects non-octal codes
  - the server-controlled pre-filter (mlat/client/acmatch.py): exclude, Mode A match on
    DF5/DF21 squawks of confirmed Mode S aircraft, never_drop codes, window expiry,
    Mode C match, settings via handshake / later message, reader DF filter switching

Run from the repo root after building the extension:
    python3 setup.py build_ext --inplace
    python3 tools/test_modeac.py
"""

import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import _modes                                    # noqa: E402
from mlat.client import jsonclient, options      # noqa: E402
from mlat.client.coordinator import Coordinator  # noqa: E402
from mlat.client.receiver import ReceiverConnection  # noqa: E402
from mlat.client.stats import global_stats       # noqa: E402

FREQ = 12_000_000


def beast(frame_type, timestamp, data, signal=0x80):
    """Build one Beast frame, escaping 0x1a bytes."""
    body = timestamp.to_bytes(6, 'big') + bytes([signal]) + bytes(data)
    return b'\x1a' + frame_type + body.replace(b'\x1a', b'\x1a\x1a')


def modes_long(timestamp):
    # any 14-byte frame advances last_timestamp, CRC validity doesn't matter for that
    return beast(b'3', timestamp, bytes.fromhex('8d4840d6202cc371c32ce0576098'))


def modeac(timestamp, word):
    return beast(b'1', timestamp, word.to_bytes(2, 'big'))


def read(frames):
    reader = _modes.Reader(_modes.BEAST)
    reader.modeac_filter = None
    reader.default_filter = [True] * 32
    _, messages, _ = reader.feed(b''.join(frames))
    return [m for m in messages if m.df == _modes.DF_MODEAC]


failures = 0


def check(name, cond, detail=''):
    global failures
    print('{0}  {1}{2}'.format('PASS' if cond else 'FAIL', name, ('  ' + detail) if detail else ''))
    if not cond:
        failures += 1


# --- reader: A/C frames emitted, timestamp adjusted ---------------------------
t0 = 10 * FREQ
ac = read([modes_long(t0), modeac(t0 + 5000, 0x4321)])
check('A/C frame emitted by reader', len(ac) == 1)
check('A/C address is hex-octal word', ac and ac[0].address == 0x4321, ac and hex(ac[0].address))
check('A/C timestamp shifted F2->F1 by 244 ticks', ac and ac[0].timestamp == t0 + 5000 - 244,
      ac and str(ac[0].timestamp - t0))
check('A/C str() is 4 hex chars (TCP JSON path)', ac and str(ac[0]) == '4321', ac and str(ac[0]))

# --- reader: lag tolerance -------------------------------------------------------
lag_50ms = read([modes_long(t0), modes_long(t0 + FREQ // 10), modeac(t0 + FREQ // 10 - FREQ // 20, 0x0123)])
check('A/C 50 ms behind newest Mode S is kept', len(lag_50ms) == 1)
lag_2s = read([modes_long(t0 + 3 * FREQ), modes_long(t0 + 3 * FREQ + 1000), modeac(t0 + FREQ, 0x0123)])
check('A/C 2 s behind newest Mode S is dropped as stale', len(lag_2s) == 0)

# --- reader: 0x1a escaping inside an A/C payload ---------------------------------
esc = read([modes_long(t0), modeac(t0 + 7000, 0x1a1a)])
check('A/C payload containing 0x1a bytes survives escaping', esc and esc[0].address == 0x1a1a)


# --- coordinator: exclude list ---------------------------------------------------
class FakeServer:
    send_split_sync = None

    def __init__(self):
        self.sent = []

    def send_mlat(self, m):
        self.sent.append(m.address)


class FakeReceiver:
    pass


server = FakeServer()
coord = Coordinator(receiver=FakeReceiver(), server=server, outputs=[], freq=FREQ,
                    allow_anon=True, allow_modeac=True, stats_interval=900,
                    forward_all_modeac=True, modeac_exclude=options.squawk_list('1200,7000'))
msgs = read([modes_long(t0),
             modeac(t0 + 100, 0x1200),   # VFR -> excluded
             modeac(t0 + 200, 0x1280),   # VFR with SPI bit -> still excluded
             modeac(t0 + 300, 0x7000),   # excluded
             modeac(t0 + 400, 0x4000),   # military training/restricted -> forwarded
             modeac(t0 + 500, 0x0563)])  # some other code -> forwarded
global_stats.reset()
coord.input_received_messages(msgs)
check('excluded squawks dropped, others forwarded', server.sent == [0x4000, 0x0563],
      str([hex(a) for a in server.sent]))
check('stats count received/excluded/sent',
      (global_stats.modeac_received, global_stats.modeac_excluded, global_stats.modeac_sent) == (5, 3, 2))

# --- UDP packing --------------------------------------------------------------------
udp = jsonclient.UdpServerConnection('127.0.0.1', 9, 0xdeadbeef)
sent_packets = []
udp.sock.close()


class FakeSock:
    def send(self, b):
        sent_packets.append(bytes(b))


udp.sock = FakeSock()
udp.send_mlat(msgs[3])
udp.flush()
pkt = sent_packets[0]
hdr = jsonclient.STRUCT_HEADER.size
rtype, delta, payload = jsonclient.STRUCT_MLAT_MODEAC.unpack_from(pkt, hdr)
check('UDP record type is TYPE_MLAT_MODEAC (7)', rtype == jsonclient.TYPE_MLAT_MODEAC == 7)
check('UDP record payload is the 2-byte A/C word', payload == b'\x40\x00')
check('UDP packet length = header + 7 bytes', len(pkt) == hdr + 7, str(len(pkt)))

# --- receiver: Beast settings ask for Mode A/C ------------------------------------
rc = ReceiverConnection('127.0.0.1', 30005, _modes.BEAST, forward_all_modeac=True)
rc.state = 'connected'
rc.reader.mode = _modes.BEAST
sent = []
rc.send = sent.append
rc.send_settings_message()
check('Beast settings enable Mode A/C (J)', sent and sent[0].endswith(b'\x1a1J'), sent and repr(sent[0]))
check('reader accepts all A/C (filter None)', rc.reader.modeac_filter is None)
rc.update_modeac_filter(set())
check('server filter updates do not narrow forward-all mode', rc.reader.modeac_filter is None)

# --- CLI squawk parsing ---------------------------------------------------------------
check("squawk_list('1200, 4000') parses", options.squawk_list('1200, 4000') == {0x1200, 0x4000})
try:
    options.squawk_list('1289')
    check('non-octal squawk rejected', False)
except Exception:
    check('non-octal squawk rejected', True)
check("squawk_list('') is empty", options.squawk_list('') == frozenset())

# --- Mode A/C pre-filter (server-controlled) --------------------------------------------
from mlat.client import acmatch                    # noqa: E402


def id13(code):
    """hex-octal Mode A word -> 13-bit identity field (inverse of acmatch.id13_to_code)."""
    v = 0
    for bit, mask in ((0x1000, 0x0010), (0x0800, 0x1000), (0x0400, 0x0020), (0x0200, 0x2000),
                      (0x0100, 0x0040), (0x0080, 0x4000), (0x0020, 0x0100), (0x0010, 0x0001),
                      (0x0008, 0x0200), (0x0004, 0x0002), (0x0002, 0x0400), (0x0001, 0x0004)):
        if code & mask:
            v |= bit
    return v


def with_ap(head, address):
    """Append address/parity: CRC of the first bytes XOR the ICAO address."""
    crc = _modes.crc(bytes(head))
    ap = crc ^ address
    return bytes(head) + ap.to_bytes(3, 'big')


def df11(ts, address):
    head = bytes([0x5d]) + address.to_bytes(3, 'big')
    crc = _modes.crc(head)
    return beast(b'2', ts, head + crc.to_bytes(3, 'big'))


def df5(ts, address, code):
    v = id13(code)
    return beast(b'2', ts, with_ap([5 << 3, 0, v >> 8, v & 0xff], address))


def df4(ts, address, alt_ft):
    n = int((alt_ft + 1000) // 25)            # Q=1, 25 ft encoding
    ac13 = ((n & 0x7e0) << 2) | ((n & 0x010) << 1) | 0x10 | (n & 0xf)   # M bit (0x40) = 0, Q bit (0x10) = 1
    return beast(b'2', ts, with_ap([4 << 3, 0, ac13 >> 8, ac13 & 0xff], address))


check('id13 round trip (squawk 4521)', acmatch.id13_to_code(id13(0x4521)) == 0x4521)
check('Mode C table: 15000 ft is a valid code', acmatch.altitude_codes(15000, 0) and
      acmatch.mode_c_altitude(acmatch.altitude_codes(15000, 0)[0]) == 15000)
check('Mode C tolerance 100 ft = 3 codes', len(acmatch.altitude_codes(15000, 100)) == 3)

m = acmatch.ModeACMatcher()
check('no settings: nothing filtered', m.check(0x1200, 0) is None)
try:
    m.configure({'exclude': {'enabled': True, 'codes': ['1289']}})
    check('bad settings rejected', False)
except ValueError:
    check('bad settings rejected, previous kept', m.check(0x1200, 0) is None)

CFG = {'exclude': {'enabled': True, 'codes': ['1200']},
       'mode_a_match': {'enabled': True, 'window_s': 60},
       'mode_c_match': {'enabled': False}}


class FakeRx:
    def __init__(self):
        self.calls = []

    def set_modeac_match_inputs(self, identity, altitude):
        self.calls.append((identity, altitude))


AIRLINER = 0xa1b2c3
fs = FakeServer()
frx = FakeRx()
coord = Coordinator(receiver=frx, server=fs, outputs=[], freq=FREQ, allow_anon=True, allow_modeac=True,
                    stats_interval=900, forward_all_modeac=True, modeac_exclude=frozenset())
coord.server_modeac_filter(CFG)
check('settings switch on identity replies for all aircraft', frx.calls[-1] == (True, False))

t1 = t0 + FREQ * 2
frames = [modes_long(t1), df11(t1 + 10, AIRLINER), df5(t1 + 20, AIRLINER, 0x3521),   # airliner squawks 3521
          df5(t1 + 30, 0x123456, 0x6123),                                             # unknown address: ignored
          df5(t1 + 35, AIRLINER, 0x4000),                                             # (a 4000 Mode S squawk)
          modeac(t1 + 40, 0x3521),         # airliner's own Mode A reply -> dropped (mode_a)
          modeac(t1 + 50, 0x35a1),         # same code with SPI -> dropped
          modeac(t1 + 60, 0x6123),         # squawk from an unconfirmed address -> sent
          modeac(t1 + 70, 0x4000),         # shared code: never dropped by match -> sent
          modeac(t1 + 80, 0x1200),         # excluded
          modeac(t1 + 90, 0x0563)]         # unrelated -> sent
reader = _modes.Reader(_modes.BEAST)
reader.modeac_filter = None
reader.default_filter = [True] * 32
_, msgs, _ = reader.feed(b''.join(frames))
global_stats.reset()
coord.input_received_messages(msgs)
check('Mode A match drops squawks of heard Mode S aircraft only',
      fs.sent == [0x6123, 0x4000, 0x0563], str([hex(a) for a in fs.sent]))
check('stats: excluded 1, Mode S squawk 2, sent 3',
      (global_stats.modeac_excluded, global_stats.modeac_matched_a, global_stats.modeac_sent) == (1, 2, 3))

now = max(coord.acmatch.squawks.values())
check('match expires after window_s', coord.acmatch.check(0x3521, now + 59) == 'mode_a' and
      coord.acmatch.check(0x3521, now + 61) is None)

# Mode C tier
coord.server_modeac_filter(dict(CFG, mode_c_match={'enabled': True, 'window_s': 30, 'tolerance_ft': 100}))
check('Mode C tier switches on altitude replies', frx.calls[-1] == (True, True))
_, msgs, _ = reader.feed(df4(t1 + 1000, AIRLINER, 35000))
coord.input_received_messages(msgs)
fl350 = acmatch.altitude_codes(35000, 0)[0]
fl351 = acmatch.altitude_codes(35100, 0)[0]
fl353 = acmatch.altitude_codes(35300, 0)[0]
nowc = max(coord.acmatch.altitudes.values())
check('Mode C match: 35000 and +/-100 ft dropped, 35300 kept',
      coord.acmatch.check(fl350, nowc) == 'mode_c' and coord.acmatch.check(fl351, nowc) == 'mode_c'
      and coord.acmatch.check(fl353, nowc) is None)

# everything off again (e.g. server sends {} or an older server sends nothing)
coord.server_modeac_filter(None)
check('no settings from server: identity/altitude inputs off, nothing filtered',
      frx.calls[-1] == (False, False) and coord.acmatch.check(0x1200, nowc) is None)

# receiver DF filter switching (the real ReceiverConnection)
rc2 = ReceiverConnection('127.0.0.1', 30005, _modes.BEAST, forward_all_modeac=True)
rc2.set_modeac_match_inputs(identity=True, altitude=False)
check('receiver passes DF5/21 for all aircraft, not DF4/20',
      rc2.default_filter[5] and rc2.default_filter[21] and not rc2.default_filter[4]
      and not rc2.default_filter[20] and rc2.default_filter[17])
check('reader shares the same filter list', rc2.reader.default_filter is rc2.default_filter
      or list(rc2.reader.default_filter) == rc2.default_filter)

# settings arriving in the handshake / later message
class FakeCoord:
    def __init__(self):
        self.got = []

    def server_modeac_filter(self, cfg):
        self.got.append(cfg)

    def server_connected(self):
        pass


sc = jsonclient.JsonServerConnection.__new__(jsonclient.JsonServerConnection)
sc.coordinator = FakeCoord()
sc.offer_zlib = False
sc.motdShown = 0
sc.heartbeat_interval = 120
sc.host = '127.0.0.1'
sc.send_rate_report = lambda r: None
sc.handle_handshake_response({'compress': 'none', 'reconnect_in': 10, 'modeac_filter': CFG})
sc.handle_connected_request({'modeac_filter': {}})
check('settings taken from handshake and later messages', sc.coordinator.got == [CFG, {}])

# --- reply-train pairing (acpair.py) ---------------------------------------------------------
from mlat.client.acpair import TrainPairer          # noqa: E402

PRI = 2500 * 12                                     # 2.5 ms in 12 MHz ticks
FL150 = acmatch.altitude_codes(15000, 0)[0]
FL310 = acmatch.altitude_codes(31000, 0)[0]


def dwell(start, codes_by_offset, n=12, pattern='AC'):
    """Replies of several aircraft to one radar beam dwell: [(ticks, word, signal)]."""
    out = []
    for k in range(n):
        mode = pattern[k % len(pattern)]
        for offset, (sq, alt, sig) in codes_by_offset.items():
            out.append((start + k * PRI + offset, sq if mode == 'A' else alt, sig))
    return sorted(out)


# unknown P on 4000 at FL150; airliner Q (squawk 2275, FL310) in the same beam, same signal,
# its replies 30 us later (outside the 5 us spacing tolerance, clear of garbling)
pz = TrainPairer()
t = 10 ** 9
for sweep in range(3):
    for tk, w, sg in dwell(t + sweep * 12 * 4_700_000 // 1, {0: (0x4000, FL150, 120), 30 * 12: (0x2275, FL310, 120)}):
        pz.add(tk, w, sg, 100.0 + sweep * 4.7)
    pz.add(t + sweep * 56_400_000 + 40 * PRI, 0x0001, 10, 100.0 + sweep * 4.7)   # flush the window
links = {(a, b) for a, b, _ in pz.links(115.0)}
check('pairer links each aircraft\'s own squawk and altitude',
      (min(0x4000, FL150), max(0x4000, FL150)) in links and (min(0x2275, FL310), max(0x2275, FL310)) in links,
      str(sorted((hex(a), hex(b)) for a, b in links)))
check('pairer does not link a neighbour 30 us away', len(links) == 2, str(len(links)))
check('partners()', pz.partners(FL150, 115.0) == [0x4000])

# signal level separates aircraft whose replies line up exactly
pz2 = TrainPairer()
for tk, w, sg in dwell(t, {0: (0x4000, FL150, 120), 1: (0x2275, FL310, 40)}):
    pz2.add(tk, w, sg, 100.0)
pz2.add(t + 40 * PRI, 0x0001, 10, 100.0)
check('different signal levels are not linked', all({a, b} in ({0x4000, FL150}, {0x2275, FL310})
                                                    for a, b, _ in pz2.links(100.0)))

# AAC interlace (A A C) still gives sandwiches
pz3 = TrainPairer()
for tk, w, sg in dwell(t, {0: (0x4000, FL150, 120)}, n=18, pattern='AAC'):
    pz3.add(tk, w, sg, 100.0)
pz3.add(t + 40 * PRI, 0x0001, 10, 100.0)
check('A A C interlace pairs too', pz3.partners(FL150, 100.0) == [0x4000])

# links expire
check('links expire after window_s', pz3.partners(FL150, 100.0 + 31) == [])

# drop rule and hints through ModeACMatcher
mm = acmatch.ModeACMatcher()
mm.configure({'exclude': {'enabled': True, 'codes': ['1200']}, 'mode_a_match': {'enabled': True},
              'reply_pairing': {'enabled': True, 'drop_paired': True, 'send_hints': True}})
mm.note_squawk(0x2275, 100.0)                       # Q is a Mode S aircraft we hear
VFR_ALT = acmatch.altitude_codes(4500, 0)[0]
for tk, w, sg in dwell(t, {0: (0x4000, FL150, 120), 30 * 12: (0x2275, FL310, 120),
                           70 * 12: (0x1200, VFR_ALT, 90)}):
    mm.observe(tk, w, sg, 100.0)
mm.observe(t + 40 * PRI, 0x0001, 10, 100.0)
check("airliner's altitude dropped by pairing", mm.check(FL310, 100.5) == 'pair')
check("1200 VFR's altitude dropped by pairing", mm.check(VFR_ALT, 100.5) == 'pair')
check("unknown's altitude kept (partner 4000 is kept)", mm.check(FL150, 100.5) is None)
check("hints: only the unknown's link (both codes kept)",
      [h[:2] for h in mm.hints(100.5)] == [sorted([acmatch.code_to_str(FL150), '4000'])], str(mm.hints(100.5)))
mm.configure({'exclude': {'enabled': True, 'codes': ['1200']}, 'mode_a_match': {'enabled': True},
              'reply_pairing': {'enabled': True, 'drop_paired': False}})
check('drop_paired off: nothing dropped by pairing', mm.check(FL310, 100.5) is None)
try:
    mm.configure({'reply_pairing': {'enabled': True, 'min_gap_us': 7000}})
    check('bad pairing settings rejected', False)
except ValueError:
    check('bad pairing settings rejected', True)

# --- burst / signal-strength correlation (accorr.py) and real-aircraft rules ----------------------
from mlat.client.accorr import BurstCorrelator      # noqa: E402

TICK = 12_000_000


def dwells(aircraft, seconds=120.0):
    """Replies from 3 rotating radars: aircraft = {name: (squawk, alt_code, signal, bearing_deg)}.
    Each radar's beam reaches an aircraft once per rotation at a time set by its bearing; while on it,
    10 interrogations 2.5 ms apart, alternating A / C. Returns time-sorted (ticks, word, signal)."""
    radars = [(4.7, 0.0), (12.0, 0.31), (4.8, 0.77)]          # rotation period, phase
    out = []
    for period, phase in radars:
        for sq, alt, sig, brg in aircraft.values():
            t0 = (phase + brg / 360.0) * period
            while t0 < seconds:
                for k in range(10):
                    out.append((int((t0 + k * 0.0025) * TICK), sq if k % 2 == 0 else alt, sig))
                t0 += period
    return sorted(out)


bc = BurstCorrelator()
bc.configure({'corr_window_s': 300})
ac = {'P': (0x4000, FL150, 120, 40.0), 'Q': (0x2275, FL310, 60, 200.0), 'R': (0x5521, FL150, 90, 300.0)}
for tk, w, sg in dwells(ac):
    bc.add(tk, w, sg, tk / TICK)
links = {(a, b) for a, b, _ in bc.links(120.0)}
want = {tuple(sorted(p)) for p in ((0x4000, FL150), (0x2275, FL310), (0x5521, FL150))}
check('correlator links each aircraft\'s codes (FL150 shared by two aircraft)', links == want,
      str(sorted((hex(a), hex(b)) for a, b in links)))

# same bearing (always in the same beam) but 6 dB apart in signal: not linked
bc2 = BurstCorrelator()
for tk, w, sg in dwells({'P': (0x4000, FL150, 120, 40.0), 'Q': (0x2275, FL310, 60, 40.0)}):
    bc2.add(tk, w, sg, tk / TICK)
cross = {(a, b) for a, b, _ in bc2.links(120.0)} - want
check('same beam, different signal level: no cross links', not cross, str(cross))

# rules applied where links are combined
class FakeLinker:
    def __init__(self, links):
        self._links = links

    def links(self, now):
        return self._links

    def partners(self, code, now):
        return []

    def add(self, *a):
        pass


FL160 = acmatch.altitude_codes(16000, 0)[0]
FL151 = acmatch.altitude_codes(15100, 0)[0]
rm = acmatch.ModeACMatcher()
rm.configure({'reply_pairing': {'enabled': True, 'sandwich': True, 'correlation': False}})
rm.pairer = FakeLinker([(0x2275, 0x5521, 30),                       # two squawks: never
                        (0x2275, FL310, 40), (0x2275, FL160, 9),    # discrete squawk: best altitude wins...
                        (FL151, 0x2275, 12),                        # ...not this: 15100 is far from 31000
                        (0x4000, FL150, 20), (0x4000, FL160, 15)])  # shared 4000: both kept
got = {tuple(sorted((a, b))) for a, b in rm.links(1.0)}
check('two squawks never link', (min(0x2275, 0x5521), max(0x2275, 0x5521)) not in got)
check('discrete squawk keeps only its strongest altitude', (min(0x2275, FL310), max(0x2275, FL310)) in got
      and (min(0x2275, FL160), max(0x2275, FL160)) not in got and (min(0x2275, FL151), max(0x2275, FL151)) not in got,
      str(sorted((hex(a), hex(b)) for a, b in got)))
check('shared 4000 keeps several altitudes', {(min(0x4000, FL150), max(0x4000, FL150)),
                                              (min(0x4000, FL160), max(0x4000, FL160))} <= got)
rm.pairer = FakeLinker([(0x2275, FL310, 40), (0x2275, acmatch.altitude_codes(31100, 0)[0], 35)])
rm._links_at = None
check('climbing: neighbouring altitude kept', len(rm.links(2.0)) == 2)

# --- squawks from the local readsb's aircraft.json ------------------------------------------------
import http.server                                   # noqa: E402
import threading                                     # noqa: E402
import time as _time                                 # noqa: E402
from mlat.client import readsbjson                   # noqa: E402

DOC = {'now': 1000.0, 'aircraft': [
    {'hex': 'a11111', 'type': 'adsb_icao', 'squawk': '2615', 'seen': 0.4},     # ADS-B
    {'hex': 'a22222', 'type': 'adsr_icao', 'squawk': '3746', 'seen': 2.0},     # UAT via readsb
    {'hex': 'a33333', 'type': 'mode_s', 'squawk': '5013', 'seen': 10.0},       # Mode S only
    {'hex': '~2a0001', 'type': 'tisb_other', 'squawk': '4000', 'seen': 1.0},   # TIS-B: never
    {'hex': 'a44444', 'type': 'tisb_icao', 'squawk': '6125', 'seen': 1.0},     # TIS-B: never
    {'hex': 'a55555', 'type': 'adsb_icao', 'squawk': '7177', 'seen': 120.0},   # stale
    {'hex': 'a66666', 'type': 'adsb_icao', 'squawk': '1289', 'seen': 1.0},     # not octal
    {'hex': 'a77777', 'type': 'adsb_icao', 'seen': 1.0}]}                      # no squawk
check('aircraft.json: squawks from ADS-B / UAT / Mode S; never TIS-B, stale or malformed',
      readsbjson.known_squawks(DOC) == {0x2615, 0x3746, 0x5013}, str(sorted(map(hex, readsbjson.known_squawks(DOC)))))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(DOC).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
lal = readsbjson.LocalAircraftList('http://127.0.0.1:%d/data/aircraft.json' % httpd.server_address[1], interval=0.2)
deadline = _time.time() + 5
while lal.latest is None and _time.time() < deadline:
    _time.sleep(0.05)

coord5 = Coordinator(receiver=FakeRx(), server=FakeServer(), outputs=[], freq=FREQ, allow_anon=True, allow_modeac=True,
                     stats_interval=900, forward_all_modeac=True, modeac_exclude=frozenset())
coord5.server_modeac_filter(CFG)
coord5.local_known = lal
coord5.take_local_known(500.0)
check('local aircraft list feeds the Mode A match', coord5.acmatch.check(0x3746, 501.0) == 'mode_a'
      and coord5.acmatch.check(0x6125, 501.0) is None)
check('each fetch is taken once', lal.take() is None)
httpd.shutdown()

dead = readsbjson.LocalAircraftList('http://127.0.0.1:9/nothing', interval=0.2)
deadline = _time.time() + 6
while dead.ok is None and _time.time() < deadline:
    _time.sleep(0.05)
check('unreachable aircraft.json: client carries on', dead.ok is False and dead.take() is None)

# --- identity-reply decoding is a fallback only ---------------------------------------------------
class StubList:
    def __init__(self, ok):
        self.ok = ok

    def take(self):
        return None


frx6 = FakeRx()
coord6 = Coordinator(receiver=frx6, server=FakeServer(), outputs=[], freq=FREQ, allow_anon=True, allow_modeac=True,
                     stats_interval=900, forward_all_modeac=True, modeac_exclude=frozenset())
coord6.local_known = StubList(ok=None)                  # not read yet: assume it will work
coord6.server_modeac_filter(CFG)
check('aircraft list in use: identity replies not requested from the reader',
      frx6.calls[-1] == (False, False) and not coord6.identity_fallback)
_, msgs6, _ = _modes.Reader(_modes.BEAST).feed(b''.join([modes_long(t1), df11(t1 + 10, AIRLINER),
                                                          df5(t1 + 20, AIRLINER, 0x3521)]))
coord6.input_received_messages(msgs6)
check('...and none decoded', not coord6.acmatch.squawks)
coord6.local_known.ok = False
coord6.take_local_known(600.0)
check('aircraft list fails: fallback switches on', frx6.calls[-1] == (True, False) and coord6.identity_fallback)
coord6.local_known.ok = True
coord6.take_local_known(610.0)
check('aircraft list back: fallback off again', frx6.calls[-1] == (False, False) and not coord6.identity_fallback)
n_calls = len(frx6.calls)
coord6.take_local_known(620.0)
check('no reader changes while nothing changes', len(frx6.calls) == n_calls)

# --- pairing only sees replies that will be sent (unless drop_paired) -------------------------------
def pairing_load(drop_paired):
    c = Coordinator(receiver=FakeRx(), server=FakeServer(), outputs=[], freq=FREQ, allow_anon=True, allow_modeac=True,
                    stats_interval=900, forward_all_modeac=True, modeac_exclude=frozenset())
    c.server_modeac_filter({'exclude': {'enabled': True, 'codes': ['1200']}, 'mode_a_match': {'enabled': True},
                            'reply_pairing': {'enabled': True, 'drop_paired': drop_paired}})
    from mlat.client.util import monotonic_time
    c.acmatch.note_squawk(0x2275, monotonic_time())  # a known airliner, heard just now
    tp = t0 + FREQ * 80
    _, ms, _ = _modes.Reader(_modes.BEAST).feed(b''.join(
        [modes_long(tp), modeac(tp + 100, 0x2275), modeac(tp + 200, 0x1200), modeac(tp + 300, 0x4000)]))
    c.input_received_messages(ms)
    return [w for _, w, _ in c.acmatch.pairer.recent]


check('drop_paired off: only replies that will be sent reach pairing', pairing_load(False) == [0x4000],
      str([hex(w) for w in pairing_load(False)]))
check('drop_paired on: pairing sees everything', pairing_load(True) == [0x2275, 0x1200, 0x4000])

# --- pair hints go out with or without a local aircraft list ------------------------------------------
class HintServer(FakeServer):
    state = 'ready'

    def __init__(self):
        FakeServer.__init__(self)
        self.hints = []

    def send_modeac_pairs(self, p):
        self.hints.append(p)


for with_list in (False, True):
    hs = HintServer()
    c7 = Coordinator(receiver=FakeRx(), server=hs, outputs=[], freq=FREQ, allow_anon=True, allow_modeac=True,
                     stats_interval=900, forward_all_modeac=True, modeac_exclude=frozenset())
    if with_list:
        c7.local_known = StubList(ok=True)
    c7.server_modeac_filter({'reply_pairing': {'enabled': True, 'send_hints': True}})
    for tk, w, sg in dwell(t, {0: (0x4000, FL150, 120)}):
        c7.acmatch.observe(tk, w, sg, 100.0)
    c7.acmatch.observe(t + 40 * PRI, 0x0001, 10, 100.0)
    c7.next_pair_hints = 0
    c7.take_local_known(100.5)
    c7.send_pair_hints(100.5)
    check('pair hints sent %s a local aircraft list' % ('with' if with_list else 'without'),
          hs.hints and [h[:2] for h in hs.hints[0]] == [sorted([acmatch.code_to_str(FL150), '4000'])], str(hs.hints))

print()
print('reconnect back-off')

from mlat.client.net import ReconnectingConnection  # noqa: E402


class BackoffConn(ReconnectingConnection):
    max_backoff = 600.0


bc = BackoffConn('example.invalid', 1)
bc.fail_streak = 0
check('first retry uses the normal interval', bc.retry_delay() == 10.0)
bc.fail_streak = 3
check('repeated failures back off', 30.0 <= bc.retry_delay() <= 40.0, str(bc.retry_delay()))
bc.fail_streak = 20
check('back-off capped at 10 min', 450.0 <= bc.retry_delay() <= 600.0, str(bc.retry_delay()))
bc.fail_streak = 1
bc.reconnect_interval = 21600
check('server-requested hold-off honoured', bc.retry_delay() == 21600)
plain = ReconnectingConnection('example.invalid', 1)
plain.fail_streak = 20
check('local receiver connection keeps retrying quickly', plain.retry_delay() == 10.0)

from mlat.client.jsonclient import JsonServerConnection  # noqa: E402

jc = object.__new__(JsonServerConnection)
try:
    jc.handle_connected_request({'deny': ['banned'], 'reconnect_in': 86400})
    check('server can end a session with a reason', False)
except IOError:
    check('server can end a session with a reason', jc.reconnect_interval == 86400)

print()
print('{0} failure(s)'.format(failures))
sys.exit(1 if failures else 0)
