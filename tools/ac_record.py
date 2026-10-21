#!/usr/bin/env python3
"""
Mode A/C recorder: keeps test datasets of a receiver's complete raw feed.

Records readsb's Beast output exactly as received (Mode S, ADS-B and Mode A/C, with 12 MHz
timestamps and signal levels), so a dataset can be replayed through anything later: the
pre-filter, the pairers, ac_capture.py, PlanePlotter... Alongside it: snapshots of readsb's
aircraft.json (what the known traffic was and where), a timestamp<->clock map, the receiver's
position and your notes.

Commands

  ring   run continuously, keeping the last --minutes in 1-minute segments (default 60):
             ac_record.py ring --connect ultrafeeder:30005 --dir /data
  save   after you notice something (a fighter on 4000...), keep the last N minutes as a dataset:
             ac_record.py save --dir /data --minutes 30 --label vipers-4000 --note "2 F-16s, FL150 per radio"
  record one-off recording straight into a dataset (no ring needed):
             ac_record.py record --connect ultrafeeder:30005 --seconds 1800 --dir /data --label test
  list   show segments in the ring and saved datasets

A dataset is a folder  <dir>/datasets/<YYYYmmdd-HHMMSS>_<label>/  containing
  beast.bin.gz       the raw Beast stream (gzip; `zcat beast.bin.gz | ...` gives the original bytes)
  aircraft.jsonl.gz  one line per poll: {"t": unix, "aircraft": [readsb aircraft.json entries]}
  clock.jsonl        one line per second: {"t": unix wall clock, "ticks": newest Beast timestamp}
  meta.json          receiver name/position, times, label, notes, sizes

Analyse with:  python3 tools/ac_capture.py <dataset folder>

Receiver name and position default to the client container's MLAT_USER / LAT / LON / ALT.
Only the Python standard library and this repo's _modes extension are used.
"""

import argparse
import glob
import gzip
import json
import os
import shutil
import socket
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import _modes                                            # noqa: E402

BEAST_SETTINGS = b'\x1a1C\x1a1d\x1a1f\x1a1J'             # binary, all frames, CRC, Mode A/C on
SEGMENT_S = int(os.environ.get('AC_RECORD_SEGMENT_S', 60))     # shorter only for testing
AIRCRAFT_FIELDS = ('hex', 'type', 'flight', 'squawk', 'alt_baro', 'alt_geom', 'lat', 'lon', 'seen',
                   'seen_pos', 'gs', 'track', 'baro_rate', 'rssi', 'category')


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S ') + msg, flush=True)


class Segment:
    """One output chunk: beast.gz + aircraft.jsonl.gz + clock.jsonl, opened for appending."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.beast = gzip.open(prefix + '.beast.gz', 'ab', compresslevel=4)
        self.aircraft = gzip.open(prefix + '.aircraft.jsonl.gz', 'at', compresslevel=6)
        self.clock = open(prefix + '.clock.jsonl', 'a')
        self.bytes = 0

    def close(self):
        for f in (self.beast, self.aircraft, self.clock):
            f.close()


class Recorder:
    def __init__(self, connect, aircraft_url, poll_s=2.0):
        self.connect = connect
        self.aircraft_url = aircraft_url
        self.poll_s = poll_s
        self.reader = _modes.Reader(_modes.BEAST)
        self.reader.default_filter = [True] * 32
        self.reader.modeac_filter = None
        self.parse_buf = b''
        self.newest_ticks = 0
        self.counts = {'modes': 0, 'modeac': 0}

    def _open(self):
        host, port = self.connect.rsplit(':', 1)
        sock = socket.create_connection((host, int(port)), timeout=10)
        sock.sendall(BEAST_SETTINGS)
        sock.settimeout(1.0)
        log('connected to {0}'.format(self.connect))
        return sock

    def _track(self, data):
        """Parse just enough to keep a timestamp<->clock map and message counts."""
        self.parse_buf += data
        try:
            n, msgs, _ = self.reader.feed(self.parse_buf)
        except ValueError:
            self.parse_buf = b''
            self.reader = _modes.Reader(_modes.BEAST)
            self.reader.default_filter = [True] * 32
            self.reader.modeac_filter = None
            return
        self.parse_buf = self.parse_buf[n:]
        for m in msgs:
            if m.timestamp:
                self.newest_ticks = max(self.newest_ticks, m.timestamp)
            if m.df == _modes.DF_MODEAC:
                self.counts['modeac'] += 1
            elif m.df < 32:
                self.counts['modes'] += 1

    def _poll_aircraft(self, seg):
        if not self.aircraft_url:
            return
        try:
            if self.aircraft_url.startswith(('http://', 'https://')):
                with urllib.request.urlopen(self.aircraft_url, timeout=2) as r:
                    doc = json.load(r)
            else:
                with open(self.aircraft_url) as f:
                    doc = json.load(f)
        except Exception:                                     # noqa: BLE001 - optional, keep recording
            return
        slim = [{k: a[k] for k in AIRCRAFT_FIELDS if k in a} for a in doc.get('aircraft', [])]
        seg.aircraft.write(json.dumps({'t': round(time.time(), 2), 'now': doc.get('now'), 'aircraft': slim},
                                      separators=(',', ':')) + '\n')

    def run(self, segment_for, until=None, on_segment_done=None):
        """Record until `until` (unix time) or forever. segment_for(t) -> path prefix for the
        segment covering wall time t; a new prefix starts a new segment."""
        sock = None
        seg, seg_prefix = None, None
        next_clock = next_poll = 0.0
        try:
            while until is None or time.time() < until:
                now = time.time()
                prefix = segment_for(now)
                if prefix != seg_prefix:
                    if seg is not None:
                        seg.close()
                        if on_segment_done:
                            on_segment_done(seg_prefix)
                    seg, seg_prefix = Segment(prefix), prefix
                if sock is None:
                    try:
                        sock = self._open()
                    except OSError as e:
                        log('connect to {0} failed ({1}), retrying in 10 s'.format(self.connect, e))
                        time.sleep(10)
                        continue
                try:
                    data = sock.recv(65536)
                    if not data:
                        raise OSError('connection closed')
                    seg.beast.write(data)
                    seg.bytes += len(data)
                    self._track(data)
                except socket.timeout:
                    pass
                except OSError as e:
                    log('lost {0} ({1}), reconnecting'.format(self.connect, e))
                    sock.close()
                    sock = None
                    self.parse_buf = b''
                    time.sleep(2)
                    continue
                if now >= next_clock and self.newest_ticks:
                    next_clock = now + 1.0
                    seg.clock.write(json.dumps({'t': round(now, 3), 'ticks': self.newest_ticks}) + '\n')
                if now >= next_poll:
                    next_poll = now + self.poll_s
                    self._poll_aircraft(seg)
        finally:
            if seg is not None:
                seg.close()
                if on_segment_done:
                    on_segment_done(seg_prefix)
            if sock is not None:
                sock.close()


# ------------------------------------------------------------------------------- datasets

def receiver_meta(args):
    return {
        'receiver': args.name or os.environ.get('MLAT_USER') or socket.gethostname(),
        'lat': _float(args.lat or os.environ.get('LAT')),
        'lon': _float(args.lon or os.environ.get('LON')),
        'alt': args.alt or os.environ.get('ALT'),
        'source': getattr(args, 'connect', None),
    }


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_dataset(segment_prefixes, out_dir, meta):
    """Concatenate segments (gzip members concatenate into one valid gzip file) into a dataset."""
    os.makedirs(out_dir, exist_ok=False)
    with open(os.path.join(out_dir, 'beast.bin.gz'), 'wb') as beast, \
            open(os.path.join(out_dir, 'aircraft.jsonl.gz'), 'wb') as ac, \
            open(os.path.join(out_dir, 'clock.jsonl'), 'wb') as clock:
        for p in segment_prefixes:
            for src, dst in ((p + '.beast.gz', beast), (p + '.aircraft.jsonl.gz', ac), (p + '.clock.jsonl', clock)):
                if os.path.exists(src):
                    with open(src, 'rb') as f:
                        shutil.copyfileobj(f, dst)
    sizes = {f: os.path.getsize(os.path.join(out_dir, f)) for f in os.listdir(out_dir)}
    meta = dict(meta, files=sizes)
    with open(os.path.join(out_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    return meta


def dataset_name(start, label):
    safe = ''.join(c if c.isalnum() or c in '-_' else '-' for c in (label or 'capture'))[:60]
    return time.strftime('%Y%m%d-%H%M%S', time.gmtime(start)) + 'Z_' + safe


def ring_segments(ring_dir):
    """[(start_unix, prefix)] of complete or in-progress segments, oldest first."""
    out = []
    for path in glob.glob(os.path.join(ring_dir, 'seg-*.beast.gz')):
        prefix = path[:-len('.beast.gz')]
        try:
            out.append((int(os.path.basename(prefix)[4:]), prefix))
        except ValueError:
            continue
    return sorted(out)


# ------------------------------------------------------------------------------- commands

def cmd_ring(args):
    ring_dir = os.path.join(args.dir, 'ring')
    os.makedirs(ring_dir, exist_ok=True)
    rec = Recorder(args.connect, args.aircraft_json)
    with open(os.path.join(ring_dir, 'receiver.json'), 'w') as f:
        json.dump(receiver_meta(args), f)

    def segment_for(t):
        return os.path.join(ring_dir, 'seg-%d' % (int(t) // SEGMENT_S * SEGMENT_S))

    def prune(_done):
        cutoff = time.time() - args.minutes * 60 - SEGMENT_S
        for start, prefix in ring_segments(ring_dir):
            if start < cutoff:
                for ext in ('.beast.gz', '.aircraft.jsonl.gz', '.clock.jsonl'):
                    try:
                        os.remove(prefix + ext)
                    except FileNotFoundError:
                        pass
        log('ring: {0} segments kept; Mode S {1}, Mode A/C {2} messages so far'.format(
            len(ring_segments(ring_dir)), rec.counts['modes'], rec.counts['modeac']))

    log('ring recorder: last {0} minutes of {1} kept in {2}'.format(args.minutes, args.connect, ring_dir))
    rec.run(segment_for, on_segment_done=prune)


def cmd_save(args):
    ring_dir = os.path.join(args.dir, 'ring')
    segs = ring_segments(ring_dir)
    if not segs:
        sys.exit('no ring segments in {0} - is "ring" running?'.format(ring_dir))
    newest = segs[-1][0]
    if newest + SEGMENT_S > time.time():
        # the current minute is still being written; it closes at the minute boundary
        wait = newest + SEGMENT_S + 3 - time.time()
        print('waiting {0:.0f} s for the current minute to finish...'.format(wait), flush=True)
        time.sleep(max(0.0, wait))
        segs = ring_segments(ring_dir)
    now = time.time()
    chosen = [(s, p) for s, p in segs if s >= now - args.minutes * 60 - SEGMENT_S and s + SEGMENT_S <= now]
    if not chosen:
        sys.exit('no segments in the last {0} minutes'.format(args.minutes))
    try:
        with open(os.path.join(ring_dir, 'receiver.json')) as f:
            rmeta = json.load(f)
    except (OSError, ValueError):
        rmeta = receiver_meta(args)
    start = chosen[0][0]
    out = os.path.join(args.dir, 'datasets', dataset_name(start, args.label))
    meta = dict(rmeta, label=args.label, note=args.note, start=start, end=round(now),
                minutes=round((now - start) / 60.0, 1), saved_at=round(now), segments=len(chosen),
                format='beast.bin.gz = raw Beast stream as received from readsb')
    meta = build_dataset([p for _, p in chosen], out, meta)
    print('saved {0} ({1:.1f} min, {2:.1f} MB)'.format(out, meta['minutes'], sum(meta['files'].values()) / 1e6))


def cmd_record(args):
    tmp = os.path.join(args.dir, 'tmp-record-%d' % os.getpid())
    os.makedirs(tmp, exist_ok=True)
    start = time.time()
    rec = Recorder(args.connect, args.aircraft_json)
    log('recording {0} for {1:.0f} s'.format(args.connect, args.seconds))
    try:
        rec.run(lambda t: os.path.join(tmp, 'seg-%d' % int(start)), until=start + args.seconds)
    except KeyboardInterrupt:
        log('stopped early')
    end = time.time()
    out = os.path.join(args.dir, 'datasets', dataset_name(start, args.label))
    meta = dict(receiver_meta(args), label=args.label, note=args.note, start=round(start), end=round(end),
                minutes=round((end - start) / 60.0, 1), counts=rec.counts,
                format='beast.bin.gz = raw Beast stream as received from readsb')
    meta = build_dataset([os.path.join(tmp, 'seg-%d' % int(start))], out, meta)
    shutil.rmtree(tmp, ignore_errors=True)
    print('saved {0} ({1:.1f} min, {2:.1f} MB; Mode S {3}, Mode A/C {4} messages)'.format(
        out, meta['minutes'], sum(meta['files'].values()) / 1e6, rec.counts['modes'], rec.counts['modeac']))


def cmd_list(args):
    segs = ring_segments(os.path.join(args.dir, 'ring'))
    if segs:
        mb = sum(os.path.getsize(p + '.beast.gz') for _, p in segs) / 1e6
        print('ring: {0} segments, {1} .. {2} UTC, {3:.1f} MB'.format(
            len(segs), time.strftime('%H:%M', time.gmtime(segs[0][0])),
            time.strftime('%H:%M', time.gmtime(segs[-1][0] + SEGMENT_S)), mb))
    else:
        print('ring: empty')
    for d in sorted(glob.glob(os.path.join(args.dir, 'datasets', '*'))):
        try:
            with open(os.path.join(d, 'meta.json')) as f:
                m = json.load(f)
            print('{0}  {1} min  {2}'.format(os.path.basename(d), m.get('minutes'), m.get('note') or ''))
        except (OSError, ValueError):
            print(os.path.basename(d), '(no meta.json)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p, connect):
        p.add_argument('--dir', default='/data', help='where the ring and datasets live (default /data)')
        if connect:
            p.add_argument('--connect', default='ultrafeeder:30005', help="readsb's Beast output (host:port)")
            p.add_argument('--aircraft-json', default='http://ultrafeeder/data/aircraft.json',
                           help="readsb aircraft.json URL (or file) to snapshot ('' to skip)")
        p.add_argument('--name', help='receiver name (default MLAT_USER)')
        p.add_argument('--lat')
        p.add_argument('--lon')
        p.add_argument('--alt')

    p = sub.add_parser('ring', help='record continuously, keep the last N minutes')
    common(p, True)
    p.add_argument('--minutes', type=float, default=60)
    p.set_defaults(func=cmd_ring)

    p = sub.add_parser('save', help='keep the last N minutes of the ring as a dataset')
    common(p, False)
    p.add_argument('--minutes', type=float, default=30)
    p.add_argument('--label', default='capture')
    p.add_argument('--note', default='')
    p.set_defaults(func=cmd_save)

    p = sub.add_parser('record', help='one-off recording into a dataset')
    common(p, True)
    p.add_argument('--seconds', type=float, default=1800)
    p.add_argument('--label', default='capture')
    p.add_argument('--note', default='')
    p.set_defaults(func=cmd_record)

    p = sub.add_parser('list', help='show the ring and saved datasets')
    common(p, False)
    p.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
