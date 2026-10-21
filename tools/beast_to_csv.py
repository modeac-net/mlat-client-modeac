#!/usr/bin/env python3
"""
beast_to_csv.py - record a receiver's raw Beast feed (Mode S, ADS-B and Mode A/C) to a CSV file.

Standalone: plain Python 3.8+, no installs, runs on Windows / Mac / Linux. Connect it to readsb /
ultrafeeder's Beast output (port 30005) on your Pi, over your LAN or Tailscale:

    python beast_to_csv.py 192.168.1.50                          (until you press Ctrl+C)
    python beast_to_csv.py 192.168.1.50 --minutes 20 --note "2 F-16s on 4000, FL150 per radio"
    python beast_to_csv.py my-pi --port 30005 --out vipers.csv.gz

The Pi's readsb must decode Mode A/C (ultrafeeder: READSB_MODEAC=true); this script also asks
for it over the connection.

Output: one CSV row per message (gzip-compressed if the name ends in .gz, the default), plus a
small <name>.json with your note and the start/end times. Columns:

  time_unix   PC clock when the message arrived (s)
  ticks       receiver's 12 MHz timestamp (precise time of arrival; used for pairing / MLAT)
  signal      Beast signal byte (0-255, amplitude)
  kind        modeac / modes_short / modes_long
  raw_hex     the message bytes (lossless: the Beast stream can be rebuilt from ticks/signal/raw_hex)
  ac_code     Mode A/C: the 4-digit code (e.g. 4000)          | blank for Mode S
  ac_spi      Mode A/C: 1 if the IDENT (SPI) bit is set
  ac_alt_ft   Mode A/C: the code read as a Mode C altitude, if it can be one (it may be a squawk!)
  df          Mode S downlink format
  icao        Mode S address (DF11/17/18: from the message; others: recovered from the parity)
  crc_ok      DF11/17/18: 1 if the CRC checks out (blank for others: no separate check possible)
  squawk      DF5/21 identity replies: the squawk
  alt_ft      DF0/4/16/20 and DF17 airborne position: barometric altitude

Size: roughly 100 bytes per message before compression; a busy receiver can produce a few thousand
messages a second, so keep recordings to 10-30 minutes. .gz cuts the size by about 3-4x.
"""

import argparse
import collections
import csv
import gzip
import json
import os
import socket
import sys
import time

BEAST_SETTINGS = b'\x1a1C\x1a1d\x1a1f\x1a1J'      # binary output, all frames, CRC check, Mode A/C on
FRAME_LEN = {0x31: 2, 0x32: 7, 0x33: 14, 0x34: 14}   # '1' Mode A/C, '2' short, '3' long, '4' status
KIND = {0x31: 'modeac', 0x32: 'modes_short', 0x33: 'modes_long'}
COLUMNS = ['time_unix', 'ticks', 'signal', 'kind', 'raw_hex', 'ac_code', 'ac_spi', 'ac_alt_ft',
           'df', 'icao', 'crc_ok', 'squawk', 'alt_ft']

# ---------------------------------------------------------------- Mode S CRC


def _crc_table():
    table = []
    for i in range(256):
        c = i << 16
        for _ in range(8):
            c = ((c << 1) ^ 0xFFF409) if c & 0x800000 else (c << 1)
        table.append(c & 0xFFFFFF)
    return table


CRC_TABLE = _crc_table()


def crc_residual(msg):
    """CRC of all but the last 3 bytes, XOR the last 3 bytes (0 for a good DF17)."""
    c = 0
    for b in msg[:-3]:
        c = ((c << 8) & 0xFFFFFF) ^ CRC_TABLE[((c >> 16) ^ b) & 0xFF]
    return c ^ int.from_bytes(msg[-3:], 'big')

# ---------------------------------------------------------------- code helpers


def id13_to_code(id13):
    """13-bit identity / altitude field -> 'hex-octal' Mode A word (squawk 4000 = 0x4000)."""
    code = 0
    for bit, mask in ((0x1000, 0x0010), (0x0800, 0x1000), (0x0400, 0x0020), (0x0200, 0x2000),
                      (0x0100, 0x0040), (0x0080, 0x4000), (0x0020, 0x0100), (0x0010, 0x0001),
                      (0x0008, 0x0200), (0x0004, 0x0002), (0x0002, 0x0400), (0x0001, 0x0004)):
        if id13 & bit:
            code |= mask
    return code


def gillham_ft(code):
    """A Mode A/C word read as a Mode C (Gillham) altitude in feet, or None if it can't be one."""
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


def ac13_ft(ac13):
    if not ac13 or ac13 & 0x40:                  # no altitude / metric
        return None
    if ac13 & 0x10:                              # Q bit: 25 ft steps
        n = ((ac13 & 0x1F80) >> 2) | ((ac13 & 0x20) >> 1) | (ac13 & 0x0F)
        return n * 25 - 1000
    return gillham_ft(id13_to_code(ac13))


def ac12_ft(ac12):
    if not ac12:
        return None
    if ac12 & 0x10:
        n = ((ac12 & 0x0FE0) >> 1) | (ac12 & 0x0F)
        return n * 25 - 1000
    return gillham_ft(id13_to_code(((ac12 & 0x0FC0) << 1) | (ac12 & 0x003F)))

# ---------------------------------------------------------------- decoding


def decode(ftype, ticks, signal, data, now):
    row = {'time_unix': '%.3f' % now, 'ticks': ticks, 'signal': signal, 'kind': KIND[ftype],
           'raw_hex': data.hex()}
    if ftype == 0x31:
        word = (data[0] << 8) | data[1]
        row['ac_code'] = '%04x' % (word & 0x7777)
        row['ac_spi'] = 1 if word & 0x0080 else 0
        alt = gillham_ft(word & 0x7777)
        row['ac_alt_ft'] = '' if alt is None else alt
        return row
    df = data[0] >> 3
    row['df'] = df
    if df in (11, 17, 18):
        row['icao'] = data[1:4].hex()
        res = crc_residual(data)
        row['crc_ok'] = 1 if (res == 0 if df != 11 else (res & ~0x7F) == 0) else 0
        if df in (17, 18) and len(data) == 14:
            tc = data[4] >> 3
            if 9 <= tc <= 18 or 20 <= tc <= 22:
                alt = ac12_ft((data[5] << 4) | (data[6] >> 4))
                row['alt_ft'] = '' if alt is None else alt
    elif df in (0, 4, 5, 16, 20, 21):
        row['icao'] = '%06x' % crc_residual(data)
        field = ((data[2] & 0x1F) << 8) | data[3]
        if df in (5, 21):
            row['squawk'] = '%04x' % id13_to_code(field)
        else:
            alt = ac13_ft(field)
            row['alt_ft'] = '' if alt is None else alt
    return row


class BeastParser:
    """Splits a Beast byte stream into (type, ticks, signal, data) frames, handling 0x1a escapes."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk):
        self.buf += chunk
        buf, out, i, n = self.buf, [], 0, len(self.buf)
        while True:
            while i < n and buf[i] != 0x1A:
                i += 1                                   # resync to the next frame start
            if i + 1 >= n:
                break
            ftype = buf[i + 1]
            if ftype not in FRAME_LEN:
                i += 1
                continue
            need = 6 + 1 + FRAME_LEN[ftype]
            body, j = bytearray(), i + 2
            while len(body) < need and j < n:
                b = buf[j]
                if b == 0x1A:
                    if j + 1 >= n:
                        break
                    if buf[j + 1] != 0x1A:              # unescaped 0x1a: frame was cut short
                        break
                    j += 1
                body.append(b)
                j += 1
            if len(body) < need:
                if j >= n or (j + 1 >= n and buf[j] == 0x1A):
                    break                                # wait for more bytes
                i += 1                                   # corrupt frame: resync
                continue
            if ftype != 0x34:
                out.append((ftype, int.from_bytes(body[0:6], 'big'), body[6], bytes(body[7:])))
            i = j
        del self.buf[:i]
        return out

# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('host', help="the Pi running readsb / ultrafeeder (name or IP)")
    ap.add_argument('--port', type=int, default=30005, help='Beast output port (default 30005)')
    ap.add_argument('--minutes', type=float, default=0, help='stop after this long (default: until Ctrl+C)')
    ap.add_argument('--out', help='output file (default modeac-<host>-<date-time>.csv.gz)')
    ap.add_argument('--note', default='', help='what you saw / heard (saved alongside)')
    ap.add_argument('--receiver', default='', help='receiver name, e.g. my-site')
    ap.add_argument('--modeac-only', action='store_true',
                    help='only keep Mode A/C rows (smaller, but loses the Mode S data used for filtering)')
    args = ap.parse_args()

    out = args.out or 'modeac-{0}-{1}.csv.gz'.format(args.host.replace(':', '_'), time.strftime('%Y%m%d-%H%M%S'))
    f = gzip.open(out, 'wt', newline='') if out.endswith('.gz') else open(out, 'w', newline='')
    writer = csv.DictWriter(f, fieldnames=COLUMNS)
    writer.writeheader()

    start = time.time()
    end = start + args.minutes * 60 if args.minutes > 0 else None
    counts = collections.Counter()
    codes = collections.Counter()
    parser = BeastParser()
    sock = None
    next_status = start + 10
    print('Recording {0}:{1} to {2} - press Ctrl+C to stop'.format(args.host, args.port, out), flush=True)
    try:
        while end is None or time.time() < end:
            if sock is None:
                try:
                    sock = socket.create_connection((args.host, args.port), timeout=10)
                    sock.sendall(BEAST_SETTINGS)
                    sock.settimeout(1.0)
                    parser = BeastParser()
                    print('connected', flush=True)
                except OSError as e:
                    print('could not connect ({0}), retrying in 10 s'.format(e), flush=True)
                    time.sleep(10)
                    continue
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    raise OSError('connection closed by the Pi')
            except socket.timeout:
                chunk = b''
            except OSError as e:
                print('lost connection ({0}), reconnecting'.format(e), flush=True)
                sock.close()
                sock = None
                time.sleep(2)
                continue
            now = time.time()
            for ftype, ticks, signal, data in parser.feed(chunk):
                if args.modeac_only and ftype != 0x31:
                    continue
                row = decode(ftype, ticks, signal, data, now)
                writer.writerow(row)
                counts[row['kind']] += 1
                if ftype == 0x31:
                    codes[row['ac_code']] += 1
            if now >= next_status:
                el = now - start
                top = ', '.join('{0} x{1}'.format(c, n) for c, n in codes.most_common(6))
                print('{0:5.0f} s  Mode S {1:6.0f}/s  Mode A/C {2:6.0f}/s   top A/C codes: {3}'.format(
                    el, (counts['modes_short'] + counts['modes_long']) / el, counts['modeac'] / el, top or 'none yet'),
                    flush=True)
                if el > 20 and not counts['modeac']:
                    print('  no Mode A/C yet: is READSB_MODEAC=true set on the Pi?', flush=True)
                next_status = now + 10
    except KeyboardInterrupt:
        print('stopping')
    finally:
        if sock is not None:
            sock.close()
        f.close()
    stop = time.time()
    meta = {'source': '{0}:{1}'.format(args.host, args.port), 'receiver': args.receiver, 'note': args.note,
            'start_unix': round(start, 1), 'end_unix': round(stop, 1),
            'start_local': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start)),
            'minutes': round((stop - start) / 60.0, 1), 'rows': dict(counts),
            'columns': COLUMNS, 'modeac_only': args.modeac_only}
    meta_path = (out[:-7] if out.endswith('.csv.gz') else os.path.splitext(out)[0]) + '.json'
    with open(meta_path, 'w') as m:
        json.dump(meta, m, indent=1)
    print('saved {0} ({1:.1f} MB, {2} rows) and {3}'.format(
        out, os.path.getsize(out) / 1e6, sum(counts.values()), meta_path))


if __name__ == '__main__':
    sys.exit(main())
