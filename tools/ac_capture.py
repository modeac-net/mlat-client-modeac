#!/usr/bin/env python3
"""
Replay a raw Beast capture through the client's Mode A/C pre-filter and reply-train pairer,
exactly as mlat-client-modeac would run them live, and report what they find.

Easiest, on the receiver itself, inside the running client container (records 15 minutes from
readsb, keeps the recording in /tmp/ac-capture.bin, then reports):

    docker exec -it mlat-modeac python3 /app/tools/ac_capture.py --connect ultrafeeder:30005 --seconds 900

Or record with netcat and analyse anywhere (from the repo root, after python3 setup.py build_ext --inplace):

    timeout 900 nc localhost 30005 > my-beast.bin
    python3 tools/ac_capture.py my-beast.bin

Reports:
  - message rates, confirmed Mode S aircraft, squawks heard from them
  - sandwich gap histogram: the radars' interrogation intervals this receiver hears
  - the strongest code links (squawk <-> altitude), with each code decoded
  - what share of Mode A/C replies each filter tier would drop, with and without pairing

Pairing settings can be tried with --min-hits, --signal-tol-db, --spacing-tol-us, ...
With --truth (the simulator's truth.jsonl) it also scores links against the real pairs.
"""

import argparse
import collections
import gzip
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import _modes                                            # noqa: E402
from mlat.client import acmatch                          # noqa: E402

FREQ = 12_000_000


class CsvAsBeast:
    """Rebuilds the Beast byte stream from a beast_to_csv.py recording (ticks, signal, raw_hex)."""

    TYPES = {'modeac': b'1', 'modes_short': b'2', 'modes_long': b'3'}

    def __init__(self, path):
        import csv
        self.f = gzip.open(path, 'rt', newline='') if path.endswith('.gz') else open(path, newline='')
        self.rows = csv.DictReader(self.f)
        self.pending = b''

    def read(self, size):
        parts, n = [self.pending], len(self.pending)
        for row in self.rows:
            t = self.TYPES.get(row['kind'])
            if t is None:
                continue
            body = int(row['ticks']).to_bytes(6, 'big') + bytes([int(row['signal'])]) + bytes.fromhex(row['raw_hex'])
            frame = b'\x1a' + t + body.replace(b'\x1a', b'\x1a\x1a')
            parts.append(frame)
            n += len(frame)
            if n >= size:
                break
        data = b''.join(parts)
        self.pending = data[size:]
        return data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()


def open_capture(path):
    """A raw Beast file, a .gz of one, a beast_to_csv.py CSV (.csv / .csv.gz), or an ac_record.py dataset."""
    if path.endswith(('.csv', '.csv.gz')):
        return CsvAsBeast(path)
    if os.path.isdir(path):
        path = os.path.join(path, 'beast.bin.gz')
    if path.endswith('.gz'):
        return gzip.open(path, 'rb')
    return open(path, 'rb')


def messages(path):
    reader = _modes.Reader(_modes.BEAST)
    reader.default_filter = [True] * 32
    reader.modeac_filter = None
    reader.want_zero_timestamps = False
    buf = b''
    with open_capture(path) as f:
        while True:
            try:
                chunk = f.read(1 << 20)
            except EOFError:                 # gzip cut short (copied while being written)
                chunk = b''
            if not chunk:
                break
            buf += chunk
            n, msgs, err = reader.feed(buf)
            buf = buf[n:]
            yield from msgs
            if err:
                raise ValueError('Beast parse error in {0}: {1}'.format(path, err))


def known_from_aircraft_json(url, seconds, out, alts):
    """While recording: poll the local readsb's aircraft.json every 10 s, as the client does, and
    collect every squawk it knows (from any source: identity replies, ADS-B, UAT). Also keeps each
    squawk's barometric altitudes, so squawk/altitude links found in the capture can be checked
    against what the aircraft actually reported."""
    from mlat.client import readsbjson
    import urllib.request
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                doc = json.load(r)
            out.update(readsbjson.known_squawks(doc))
            for a in doc.get('aircraft', []):
                sq, alt = a.get('squawk'), a.get('alt_baro')
                if (sq and isinstance(alt, (int, float)) and a.get('type') not in readsbjson.IGNORE_TYPES
                        and not str(a.get('hex', '')).startswith('~')):
                    try:
                        alts.setdefault(int(str(sq), 16), set()).add(int(alt))
                    except ValueError:
                        pass
        except Exception:                      # noqa: BLE001 - optional; replay still works without it
            pass
        time.sleep(10)


def link_verdict(a, b, alts, tol_ft=150):
    """'right' / 'wrong' if one code is a squawk aircraft.json knows and the other's Mode C altitude
    does / doesn't match an altitude that aircraft reported; None if it can't be judged."""
    verdicts = []
    for sq, other in ((a, b), (b, a)):
        if sq in alts:
            alt = acmatch.mode_c_altitude(other)
            if alt is None:
                verdicts.append('wrong')      # a known squawk linked to something that isn't an altitude
            else:
                verdicts.append('right' if any(abs(alt - x) <= tol_ft for x in alts[sq]) else 'wrong')
    if not verdicts:
        return None
    return 'right' if 'right' in verdicts else 'wrong'


def record(hostport, seconds, path):
    """Record a Beast stream (asking for Mode A/C, like mlat-client does) into a file."""
    host, port = hostport.rsplit(':', 1)
    sock = socket.create_connection((host, int(port)), timeout=10)
    sock.sendall(b'\x1a1C\x1a1d\x1a1f\x1a1J')       # Beast settings: binary, no filter, CRC, Mode A/C
    end = time.monotonic() + seconds
    n = 0
    print('Recording {0} for {1:.0f} s into {2} ...'.format(hostport, seconds, path), flush=True)
    with open(path, 'wb') as f:
        while time.monotonic() < end:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            f.write(data)
            n += len(data)
    sock.close()
    print('Recorded {0:.1f} MB'.format(n / 1e6))


def describe(code):
    alt = acmatch.mode_c_altitude(code)
    return acmatch.code_to_str(code) + (' (FL%03d)' % (alt // 100) if alt is not None else '')


def truth_pairs(path):
    """Simulator truth: {(squawk_code, altitude_code)} actually flown, per aircraft."""
    pairs, by_code = set(), collections.defaultdict(set)
    with open(path) as f:
        f.readline()
        for line in f:
            d = json.loads(line)
            sq = acmatch.code_from_str(d['squawk'])
            for k in (-1, 0, 1):
                for ac in acmatch.altitude_codes(d['alt'] + 100 * k, 0):
                    pairs.add((min(sq, ac), max(sq, ac)))
                    by_code[ac].add(d['name'])
            by_code[sq].add(d['name'])
    return pairs, by_code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('capture', nargs='?', default='/tmp/ac-capture.bin',
                    help='Beast capture: raw file, .gz, a beast_to_csv.py .csv/.csv.gz, or an ac_record.py '
                         'dataset folder (with --connect, recorded there first)')
    ap.add_argument('--connect', metavar='HOST:PORT', help='record from readsb\'s Beast output first')
    ap.add_argument('--seconds', type=float, default=900, help='recording length with --connect')
    ap.add_argument('--min-hits', type=int, default=3)
    ap.add_argument('--window-s', type=float, default=30)
    ap.add_argument('--signal-tol-db', type=float, default=3.0)
    ap.add_argument('--spacing-tol-us', type=float, default=5)
    ap.add_argument('--stagger-test', action='store_true', help='also try the staggered-PRI test')
    ap.add_argument('--min-gap-us', type=float, default=1500)
    ap.add_argument('--max-gap-us', type=float, default=6000)
    ap.add_argument('--method', choices=('sandwich', 'correlation', 'both'), default='both',
                    help='which linker(s) feed the drop rule (both are always scored)')
    ap.add_argument('--burst-gap-ms', type=float, default=25)
    ap.add_argument('--burst-signal-tol-db', type=float, default=2.0)
    ap.add_argument('--min-bursts', type=int, default=4)
    ap.add_argument('--min-ratio', type=float, default=0.4)
    ap.add_argument('--corr-window-s', type=float, default=120)
    ap.add_argument('--exclude', default='1200')
    ap.add_argument('--links', type=int, default=25, help='how many links to list')
    ap.add_argument('--truth', help="simulator truth.jsonl, to score the links")
    ap.add_argument('--aircraft-json', default=None,
                    help="with --connect: the local readsb's aircraft.json, polled while recording so the "
                         "Mode A match knows every squawk readsb knows, as the live client does "
                         "(default: http://<host>/data/aircraft.json; 'none' to skip)")
    args = ap.parse_args()

    known_json = set()
    known_alts = {}
    if args.connect:
        url = args.aircraft_json or 'http://{0}/data/aircraft.json'.format(args.connect.rsplit(':', 1)[0])
        if url.lower() != 'none':
            import threading
            threading.Thread(target=known_from_aircraft_json, args=(url, args.seconds, known_json, known_alts),
                             daemon=True).start()
        record(args.connect, args.seconds, args.capture)

    pairing = {'enabled': True, 'drop_paired': True, 'send_hints': True,
               'min_hits': args.min_hits, 'window_s': args.window_s, 'signal_tol_db': args.signal_tol_db,
               'spacing_tol_us': args.spacing_tol_us, 'stagger_test': args.stagger_test,
               'min_gap_us': args.min_gap_us, 'max_gap_us': args.max_gap_us,
               'sandwich': True, 'correlation': True,
               'burst_gap_ms': args.burst_gap_ms, 'burst_signal_tol_db': args.burst_signal_tol_db,
               'min_bursts': args.min_bursts, 'min_ratio': args.min_ratio, 'corr_window_s': args.corr_window_s}
    exclude = {'enabled': bool(args.exclude), 'codes': [c for c in args.exclude.split(',') if c]}
    m = acmatch.ModeACMatcher()
    m.configure({'exclude': exclude, 'mode_a_match': {'enabled': True}, 'reply_pairing': pairing})
    m.pairer.gap_hist = collections.Counter()
    # both linkers always run (so both can be scored); --method picks which one(s) drive the drop rule
    drive = {'sandwich': (True, False), 'correlation': (False, True), 'both': (True, True)}[args.method]

    m.use_sandwich, m.use_corr = drive          # the drop rule sees the chosen method(s)...

    def check(word, now):
        return m.check(word, now)

    confirmed = set()
    squawks_heard = {}
    counts = collections.Counter()
    first = last = None
    n_modes = n_ac = 0
    pair_dropped_codes = collections.Counter()
    # ...but both linkers are always fed, so both can be scored at the end
    def observe(ticks, word, signal, now):
        m.pairer.add(ticks, word, signal, now)
        m.correlator.add(ticks, word, signal, now)

    next_json_note = None
    for msg in messages(args.capture):
        if msg.timestamp == 0:
            continue
        now = msg.timestamp / FREQ
        first = now if first is None else first
        last = now
        # squawks from aircraft.json (collected while recording), kept fresh as the live client would
        if known_json and (next_json_note is None or now >= next_json_note):
            for code in known_json:
                m.note_squawk(code, now)
            next_json_note = now + 10.0
        if msg.df == _modes.DF_MODEAC:
            n_ac += 1
            observe(msg.timestamp, msg.address, msg.signal, now)
            reason = check(msg.address, now)
            counts[reason or 'sent'] += 1
            if reason == 'pair':
                pair_dropped_codes[msg.address & 0x7777] += 1
            continue
        n_modes += 1
        if not msg.valid:
            continue
        if msg.df in (11, 17):
            confirmed.add(msg.address)
        elif msg.df in (5, 21) and msg.address in confirmed:
            code = acmatch.squawk_from_frame(bytes(msg))
            if code is not None:
                m.note_squawk(code, now)
                squawks_heard[code] = msg.address

    if not n_ac:
        print('No Mode A/C replies in the capture: is READSB_MODEAC=true set on this receiver?')
        return 1
    dur = max(last - first, 1e-9)
    print('Capture: {0:.0f} s, {1:.0f} Mode S msg/s, {2:.0f} Mode A/C replies/s'.format(dur, n_modes / dur, n_ac / dur))
    print('Mode S aircraft confirmed: {0}, with squawk heard (DF5/21): {1}{2}'.format(
        len(confirmed), len(squawks_heard),
        ', squawks known from aircraft.json: {0}'.format(len(known_json)) if known_json else ''))
    print()

    print('Sandwich gaps (= radar interrogation intervals heard), top bins:')
    total_sw = sum(m.pairer.gap_hist.values()) or 1
    for gap, n in m.pairer.gap_hist.most_common(10):
        print('  {0:5d} us  {1:6d}  {2:5.1f}%'.format(gap, n, 100.0 * n / total_sw))
    print('  sandwiches: {0}'.format(m.pairer.sandwiches))
    print()

    # each method on its own, through the same real-aircraft rules the client applies
    def method_links(sandwich, corr):
        m.use_sandwich, m.use_corr, m._links_at = sandwich, corr, None
        try:
            return m.links(last)
        finally:
            m.use_sandwich = m.use_corr = True
            m._links_at = None
    sw = method_links(True, False)
    co = method_links(False, True)
    keys = sorted(set(sw) | set(co), key=lambda k: -(sw.get(k, 0) + co.get(k, 0)))
    print('Linked code pairs at end of capture: {0} by sandwich, {1} by correlation, {2} by both '
          '(showing {3})'.format(len(sw), len(co), len(set(sw) & set(co)), min(len(keys), args.links)))
    print('  {0:<14s}     {1:<14s} {2:>8s} {3:>8s}'.format('code', 'code', 'sandwich', 'bursts'))
    for a, b in keys[:args.links]:
        tags = []
        for c in (a, b):
            r = m._drops(c, last)
            tags.append('dropped:' + r if r else 'kept')
        v = link_verdict(a, b, known_alts) if known_alts else None
        print('  {0:<14s} <-> {1:<14s} {2:>8s} {3:>8s}   {4} / {5}{6}'.format(
            describe(a), describe(b), str(sw.get((a, b), '-')), str(co.get((a, b), '-')), *tags,
            '   ' + v.upper() if v else ''))
    print('  bursts seen: {0}'.format(m.correlator.burst_count))
    if known_alts:
        print()
        print('Links checked against aircraft.json (a known squawk and its reported altitude, +-150 ft):')
        for name, found in (('sandwich', sw), ('correlation', co)):
            judged = [link_verdict(a, b, known_alts) for (a, b) in found]
            right, wrong = judged.count('right'), judged.count('wrong')
            print('  {0:12s} {1:4d} right  {2:4d} wrong  {3:4d} not checkable (no known squawk)'.format(
                name, right, wrong, judged.count(None)))
    print()

    def pct(k):
        return 100.0 * counts[k] / n_ac
    print('Mode A/C replies, as the client would handle them (drop rule fed by: {0}; live, warming up '
          'from the start):'.format(args.method))
    for k in ('exclude', 'mode_a', 'pair', 'sent'):
        print('  {0:8s} {1:7d}  {2:5.1f}%'.format(k, counts[k], pct(k)))
    print('  sent without pairing would be {0:.1f}%'.format(pct('sent') + pct('pair')))

    if args.truth:
        tp, by_code = truth_pairs(args.truth)
        print()
        for name, found in (('sandwich', sw), ('correlation', co), ('either', {**co, **sw})):
            right = sum(1 for k in found if k in tp)
            print('Truth, {0}: {1}/{2} links are real squawk/altitude pairs'.format(name, right, len(found)))
            for (a, b), n in found.items():
                if (a, b) not in tp:
                    print('  WRONG {0} <-> {1} ({2})  (codes belong to {3} / {4})'.format(
                        describe(a), describe(b), n, sorted(by_code.get(a, ())), sorted(by_code.get(b, ()))))
        with open(args.truth) as f:
            header = json.loads(f.readline())
        target_names = {name for name, _ in header['targets']}
        wrongly = {c: n for c, n in pair_dropped_codes.items() if by_code.get(c, set()) & target_names}
        print('Replies of Mode A/C-only targets dropped by pairing: {0}'.format(
            sum(wrongly.values()) if wrongly else 0) + (
            '  ' + ', '.join('%s x%d (%s)' % (describe(c), n, '/'.join(sorted(by_code[c]))) for c, n in wrongly.items())
            if wrongly else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
