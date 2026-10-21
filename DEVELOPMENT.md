# Developing mlat-client-modeac

How this fork differs from [wiedehopf/mlat-client](https://github.com/wiedehopf/mlat-client), and the
tools that come with it. Volunteers don't need any of this: see the [README](README.md).

## How Mode A/C forwarding works

Stock mlat-client discards Mode A/C replies. This fork forwards them to a Mode A/C-capable mlat-server
(the [modeac.net](https://modeac.net) server), so aircraft whose transponders only answer Mode 3/A and
Mode C can be multilaterated.

Clock sync is unchanged: it still comes from ADS-B aircraft, exactly like stock mlat-client. Mode A/C
replies are timestamped by the same receiver clock and ride on that sync.

The client only sends Mode A/C to a server given explicitly with `--server`, and only with
`--forward-modeac`, so it can never send them to another network's server by accident. A stock
mlat-server (ADSBx, adsb.fi…) doesn't understand the new message type; keep your normal ultrafeeder
`mlat,...` entries for those networks, which are unaffected.

## Pre-filter (set by the server)

Most Mode A/C replies come from ordinary Mode S / ADS-B aircraft. To save upload and server CPU, the
client drops replies it can already explain, using the Mode S traffic it hears itself, like readsb's
`trackMatchAC()` (which readsb only uses for its console display):

* **exclude**: listed codes (default 1200)
* **Mode A match**: a code equal to the squawk of an aircraft this receiver knows about, from the last 60 s:
  squawks from DF5/DF21 identity replies (address confirmed by DF11/DF17 as the error check), plus every
  squawk the local readsb has decoded itself, read every 10 s from its aircraft.json on the same machine (ADS-B status,
  ADS-R, UAT from dump978...; TIS-B entries are ignored). Nothing is downloaded from the network; if the
  aircraft list can't be read, the client falls back to decoding identity replies itself (only then: while the
  list works, those messages aren't even passed up from the C reader). Shared codes (4000, 7000, emergencies…) are
  never dropped this way.
* **Mode C match**: a code equal to the altitude (±100 ft) of a Mode S aircraft heard recently (off by default)
* **Reply pairing** links one aircraft's squawk and altitude codes at this receiver, two ways:
  - *sandwich* (`mlat/client/acpair.py`): while a radar's beam is on an aircraft it alternates Mode A and Mode C
    interrogations a few ms apart, so this receiver hears squawk, altitude, squawk… at the radar's exact spacing
    and the same signal level. Several such replies in a row link the two codes within one beam pass.
  - *correlation* (`mlat/client/accorr.py`): the two codes are heard in the same beam dwells at the same mean
    signal level, dwell after dwell, radar after radar (like PlanePlotter's "ping-rate correlation", plus signal
    level). Slower (tens of seconds) but independent of radar timing, so it also covers staggered-PRI radars.

  Links follow two real-aircraft rules: two squawk-only codes never link, and a discrete squawk keeps only its
  strongest altitude (plus neighbouring altitudes while climbing); shared codes like 4000 may have several. Then:
  `drop_paired` drops an altitude reply whose every linked code is dropped (known Mode S traffic, 1200 VFR),
  so an unknown on 4000 keeps its altitude even at a busy flight level; `send_hints` reports links between
  kept codes (an unknown's squawk and altitude) so ac-filter pairs them at once.
  `tools/ac_capture.py` runs the same code on 15 minutes of real traffic and reports the radars' intervals,
  the links found, and how much each tier would drop:
  `docker exec -it mlat-modeac python3 /app/tools/ac_capture.py --connect ultrafeeder:30005 --seconds 900`

The server sends the settings at connect and whenever they change; volunteers never configure it. With
no settings from the server, nothing is filtered. The stats line shows each tier and the share sent (example numbers):
`Mode A/C: 812.0 msg/s received 40.1 excluded 410.3 Mode S squawk 0.0 Mode S alt 200.0 paired 161.6 msg/s sent to server (20%)`

## Recording test data on a desktop (simplest)

`tools/beast_to_csv.py` is a standalone script (plain Python 3, no installs, works on Windows) that connects to
a Pi's Beast output and writes every message to a CSV (`.csv.gz`), with your note in a small `.json` next to it:

```sh
python beast_to_csv.py 192.168.1.50 --minutes 20 --receiver my-site --note "2 F-16s on 4000, FL150 per radio"
```

Columns include the receiver timestamp, signal level and raw bytes (lossless), plus decoded Mode A/C code and
altitude reading, Mode S address, squawk and altitude. `tools/ac_capture.py` reads these CSVs directly.

## Recorder: keep test datasets

`tools/ac_record.py` keeps the receiver's complete raw feed (Mode S, ADS-B and Mode A/C, with timestamps and
signal levels) in a rolling buffer, so when you notice something you can keep the last N minutes as a dataset
to replay through the pre-filter, pairers and anything else later. Add this next to `mlat-modeac`:

```yaml
  modeac-recorder:
    image: ghcr.io/modeac-net/mlat-client-modeac
    container_name: modeac-recorder
    restart: unless-stopped
    entrypoint: ["python3", "/app/tools/ac_record.py"]
    command: ["ring", "--connect", "ultrafeeder:30005", "--minutes", "60"]
    environment:
      - MLAT_USER=${FEEDER_NAME}
      - LAT=${FEEDER_LAT}
      - LON=${FEEDER_LONG}
      - ALT=${FEEDER_ALT_M}m
    volumes:
      - ./modeac-recordings:/data
```

When a fighter is around (or just after), keep the last 30 minutes:

```sh
docker exec modeac-recorder python3 /app/tools/ac_record.py save --minutes 30 --label vipers-4000 \
    --note "2 F-16s on 4000, FL150 per radio"
docker exec modeac-recorder python3 /app/tools/ac_record.py list
docker exec modeac-recorder python3 /app/tools/ac_capture.py /data/datasets/<name>     # analyse it
```

`save` waits up to a minute for the current minute to finish. Datasets land in `./modeac-recordings/datasets/`:
`beast.bin.gz` (the raw stream, byte for byte), `aircraft.jsonl.gz` (readsb's aircraft.json every 2 s: known
traffic and positions), `clock.jsonl` (receiver timestamp ↔ wall clock), `meta.json` (receiver, position, label,
note). My rough estimate is 1–3 MB per minute compressed, depending on traffic; the ring deletes anything older than
`--minutes`. For a one-off without the ring: `ac_record.py record --seconds 1800 --label test`.

## What changed from upstream

| File | Change |
|---|---|
| `modes_reader.c` | Stop discarding Beast type-`1` (Mode A/C) frames; accept A/C frames that lag the newest Mode S timestamp by up to 1 s (readsb demodulates A/C in a separate pass, so they arrive slightly out of order); older ones are dropped as stale. A/C frames never move `last_timestamp`. |
| `mlat/client/receiver.py` | `forward_all_modeac`: reader accepts every A/C code; Beast settings always request Mode A/C (`J`); server-requested A/C filters don't narrow it. `set_modeac_match_inputs()` passes DF5/21 (and DF0/4/16/20 for Mode C match) for all aircraft when the pre-filter needs them. |
| `mlat/client/coordinator.py` | `received_modeac()` forwards every A/C reply (optionally minus `--modeac-exclude` codes; none by default, filtering is server-side) (matched on `word & 0x7777`, so the SPI bit doesn't defeat the exclude). |
| `mlat/client/jsonclient.py` | New UDP record `TYPE_MLAT_MODEAC = 7` (`>Bi2s`: type, timestamp delta, 2-byte word); pre-filter settings from the handshake response and later `modeac_filter` messages. |
| `mlat-client`, `mlat/client/options.py` | `--forward-modeac`, `--modeac-exclude`; handshake carries `"modeac_forward": true`; refuses to run with `--forward-modeac` unless `--server` is given explicitly, so A/C can never go to the ADSBx default server by accident. |
| `mlat/client/net.py` | Server connection backs off while attempts keep failing (10 s doubling to 10 min, jittered), so a whole network of clients doesn't hammer the server during an outage. The local receiver connection still retries quickly. |
| `mlat/client/jsonclient.py` (session end) | A server `deny` mid-session is logged with its reason and its `reconnect_in` honoured (e.g. a banned receiver waits a day). |
| `mlat/client/stats.py` | Mode A/C received / excluded / sent counters in the periodic stats log. |
| `mlat/client/acmatch.py` | Server-controlled pre-filter (exclude / Mode A match / Mode C match / reply pairing). |
| `mlat/client/readsbjson.py` | Reads squawks from the local readsb's aircraft.json (background thread; local only). |
| `mlat/client/acpair.py` | Reply-train pairing of squawk and altitude codes from one receiver's timing and signal level. |
| `mlat/client/accorr.py` | Burst / signal-strength correlation of squawk and altitude codes. |
| `tools/ac_capture.py` | Runs the pre-filter and both pairers over a capture or dataset and reports (and, with `--truth`, scores) what they find. |
| `tools/ac_record.py` | Recorder: rolling buffer of the raw feed, saved as replayable datasets. |
| `tools/beast_to_csv.py` | Standalone desktop recorder: Beast feed to CSV (no installs). |
| `tools/test_modeac.py` | Offline tests (run in the Docker build). |

## Wire protocol (for the server side)

* **Handshake:** client JSON includes `"modeac_forward": true`.
* **UDP:** record type `7`, struct `>Bi2s` → `(7, delta, word)`. `delta` is relative to the packet
  header timestamp, same as `TYPE_MLAT_SHORT`/`LONG`. Timestamp is already shifted to F1
  (start of the reply), same convention as Mode S frames.
* **TCP fallback:** unchanged message `{"mlat":{"t":<timestamp>,"m":"<hex>"}}`; Mode A/C is the case
  where `m` is **4 hex characters** (2 bytes) instead of 14 or 28.
* **Filtering is the server's job.** The client forwards every A/C reply by default, so 1200 / ADS-B-explained
  codes should be dropped at server ingress. Note squawk 1200 has C1–C4 all zero, which is never a valid
  Gillham altitude, so dropping 1200 never discards Mode C data.
* **The 2-byte word** is readsb's hex-octal Mode A word: `word & 0x7777` is the code
  (squawk 4000 → `0x4000`), `word & 0x0080` is SPI. A Mode C reply looks the same: decode
  it as a Gillham altitude to tell (readsb's `modeAToModeC()` does this).
  Mode A and Mode C replies cannot be told apart from the frame alone.

## Building

```sh
python3 setup.py build_ext --inplace
python3 tools/test_modeac.py
docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 -t ghcr.io/modeac-net/mlat-client-modeac:latest --push .   # normally done by .github/workflows/docker.yml
```
