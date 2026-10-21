# modeac.net receiver client

**[modeac.net](https://modeac.net)** is a volunteer network that finds aircraft other trackers can't:
aircraft whose transponders only answer in the old **Mode A/C** format, with no ADS-B and no Mode S.
Many military aircraft fly like this. When four or more volunteer receivers hear the same reply,
the server works out where the aircraft is by **multilateration (MLAT)**, and it appears on the
public map at [modeac.net](https://modeac.net), next to ordinary MLAT traffic.

This repository is the client that runs next to your receiver. It's a fork of
[wiedehopf/mlat-client](https://github.com/wiedehopf/mlat-client) (see [Credits](#credits-and-license)).

> **Private beta: Ohio only.** During the beta, the server accepts receivers in Ohio only.
> Receivers elsewhere are politely refused, and the client's log says why. The rest of the
> US opens once the beta is done.

---

## Join with ultrafeeder (about 5 minutes)

If your receiver runs **[ultrafeeder](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder)**
in Docker, which most setups following the SDR-Enthusiasts guide do, joining is three small
edits to the `docker-compose.yml` you already have. **Your other feeds (ADSBx, adsb.lol, FlightAware,
FR24…) keep running exactly as before.**

### What you need

- A receiver **in Ohio** (during the beta), with its **antenna position and height above sea level**
  already in your `.env` (the usual `FEEDER_LAT`, `FEEDER_LONG`, `FEEDER_ALT_M`). The more accurate
  these are, the better your receiver's timing is: use the antenna's position, not your street address.
- An SDR with a **TCXO**, which is any recent "ADS-B" dongle: RTL-SDR Blog V3/V4, FlightAware Pro Stick
  (Plus), Nooelec SMArt / NESDR with TCXO, Airspy… Very cheap dongles without a TCXO drift too much
  for MLAT. If your receiver already does MLAT for adsb.lol, ADSBx or adsb.fi, it's fine.

### 1. Turn on Mode A/C in ultrafeeder

In your `docker-compose.yml`, find the **`ultrafeeder:`** service and add one line to its `environment:`:

```yaml
      - READSB_MODEAC=true
```

That tells readsb to decode Mode A/C replies too. It uses a little more CPU; even a Pi 3 copes.

### 2. Send your map feed to modeac.net

Still in the `ultrafeeder:` service, find the **`ULTRAFEEDER_CONFIG`** line and add this to the end,
right after the last entry (entries are separated by `;`):

```
;adsb,feed.modeac.net,30004,beast_reduce_plus_out
```

It's the same reduced feed every aggregator gets. It puts your ADS-B traffic on the map, and tells the
server which aircraft are already known, so they aren't shown twice.

> **Where's `ULTRAFEEDER_CONFIG`?** Usually it's written out in `docker-compose.yml` as
> `- ULTRAFEEDER_CONFIG=adsb,feed.adsb.lol,30004,beast_reduce_plus_out;adsb,...`. If yours says
> `- ULTRAFEEDER_CONFIG=${ULTRAFEEDER_CONFIG}`, the list is in your `.env` file instead: add it there.

### 3. Paste the modeac.net client

Paste this block into the same `docker-compose.yml`, under `services:`, next to `ultrafeeder:`
(it must be indented the same as `ultrafeeder:`):

```yaml
  mlat-modeac:
    image: ghcr.io/modeac-net/mlat-client-modeac
    container_name: mlat-modeac
    restart: unless-stopped
    environment:
      - MLAT_SERVER=feed.modeac.net:31090
      - MLAT_USER=${FEEDER_NAME}
      - LAT=${FEEDER_LAT}
      - LON=${FEEDER_LONG}
      - ALT=${FEEDER_ALT_M}m
      - UUID=${ULTRAFEEDER_UUID:-}
```

It reads your name, location and feeder ID from the `.env` file ultrafeeder already uses, so it
normally needs **no editing**. (If your ultrafeeder service has a different name, add
`- BEAST_HOST=<that name>`.) A complete example is in [`docker-compose.example.yml`](docker-compose.example.yml).

### 4. Start it

```sh
docker compose up -d
```

### 5. Check it's working

```sh
docker logs -f mlat-modeac
```

Within a minute you should see **`Handshake complete`**. Every 5 minutes there's a **`Mode A/C:`**
line showing how many replies were received and how many were sent. Press Ctrl+C to stop watching
(the client keeps running).

Your receiver's traffic shows up on [modeac.net](https://modeac.net) within a minute or two.
Mode A/C-only aircraft only appear where four receivers hear the same aircraft, so in the beta
they'll be rare at first. Every receiver that joins fills in more of the map.

### If something's wrong

| What you see in the log | What it means / what to do |
|---|---|
| `LAT and LON must be set` or `ALT must be set` | The `.env` doesn't have `FEEDER_LAT` / `FEEDER_LONG` / `FEEDER_ALT_M`. Put your values straight into the block instead, e.g. `- LAT=40.0123`, `- LON=-83.0456`, `- ALT=250m` |
| `Mode A/C:    0.0 msg/s received` (stays at 0) | readsb isn't decoding Mode A/C: check step 1, then `docker compose up -d` again |
| `Server explicitly rejected our connection` … `outside the area this server covers` | The location in your `.env` isn't in Ohio. Often it's a missing minus sign on the longitude (Ohio's is about **-80 to -85**). Fix it, then `docker compose up -d --force-recreate mlat-modeac`. Otherwise the client waits hours before trying again |
| `Connection to feed.modeac.net:31090 failed` | Usually temporary: the client retries by itself, waiting longer each time |
| `Handshake complete`, but your planes aren't on the map | Check the step 2 line is in `ULTRAFEEDER_CONFIG`, and look at `docker logs ultrafeeder` for errors mentioning `feed.modeac.net` |

Stuck? Get in touch (see [modeac.net](https://modeac.net)) and include the last 30 lines of `docker logs mlat-modeac`.

### Updating

```sh
docker compose pull mlat-modeac && docker compose up -d
```

Or let [Watchtower](https://containrrr.dev/watchtower/) do it automatically, as many ultrafeeder setups already do.

### Leaving

Remove the `mlat-modeac:` block, the `READSB_MODEAC=true` line and the `feed.modeac.net` entry, then
run `docker compose up -d --remove-orphans`. That's all; nothing else on your system was changed.

---

## What gets sent, and what's shown

- **To the server:** your receiver's name (`FEEDER_NAME`), position and height, feeder ID, timing
  messages (Mode S and Mode A/C replies with timestamps), and, through step 2, the same reduced feed other
  aggregators get. The server also sees your internet address, like any website does.
- **On the public map:** aircraft only. **Receiver names and exact locations are never published.**
- **Why the location is checked:** MLAT only works if every receiver's position is right, so the server
  compares where you say you are with what your receiver actually hears. Receivers that don't match are
  turned away automatically. That's also how the beta stays inside Ohio.
- **Filtering:** most Mode A/C replies come from ordinary airliners the network already knows. The client
  drops those before sending, to save your upload and the server's work. The server decides the
  settings; there's nothing for you to configure.

---

## Not using ultrafeeder?

The client works with any **readsb** that has Mode A/C decoding on (`--modeac`) and a Beast output
(port 30005 by default). It only needs Docker:

```sh
docker run -d --name mlat-modeac --restart unless-stopped \
  -e MLAT_SERVER=feed.modeac.net:31090 -e MLAT_USER=my-site \
  -e LAT=40.0123 -e LON=-83.0456 -e ALT=250m \
  -e BEAST_HOST=192.168.1.50 -e BEAST_PORT=30005 \
  ghcr.io/modeac-net/mlat-client-modeac
```

Also send readsb's ADS-B feed to `feed.modeac.net:30004` (Beast, ideally `beast_reduce_plus_out`).
Only readsb has been tested. dump1090-fa (PiAware) can decode Mode A/C with `--modeac`, but it hasn't
been tried with modeac.net yet; if you try it, let us know how it goes.

### Optional settings

| Variable | Default | Meaning |
|---|---|---|
| `BEAST_HOST` | `ultrafeeder` | Container or host serving the Beast output |
| `BEAST_PORT` | `30005` | Beast output port |
| `UUID` | derived from name + position | Fixed feeder ID (the ultrafeeder block uses your `ULTRAFEEDER_UUID`) |
| `AIRCRAFT_JSON` | `http://$BEAST_HOST/data/aircraft.json` | readsb's local aircraft list, used to drop known traffic. Empty = don't use |
| `MODEAC_EXCLUDE` | none | Extra squawks to drop before sending. Normally leave unset; the server decides |
| `EXTRA_ARGS` | — | Passed straight to mlat-client |

---

## For developers

How the Mode A/C forwarding, pre-filter and reply pairing work, the wire protocol, what changed from
upstream, the recording tools and how to build: **[DEVELOPMENT.md](DEVELOPMENT.md)**.

## Credits and license

This is a fork of **[wiedehopf/mlat-client](https://github.com/wiedehopf/mlat-client)**, maintained by
wiedehopf (Matthias Wirth), which continues **[mutability/mlat-client](https://github.com/mutability/mlat-client)**
by Oliver Jowett. Clock synchronisation, the Beast reader and everything else that makes MLAT work is their
work. modeac.net adds Mode A/C forwarding, the server-controlled pre-filter and the Docker packaging. The
original README is kept in [README-upstream.md](README-upstream.md).

Please don't report problems with this fork to wiedehopf or mutability; open an issue here instead.

Licensed under the **GNU General Public License v3.0 or later**, like upstream: see [LICENSE](LICENSE)
and [NOTICE](NOTICE). The Docker image is built from exactly this source by GitHub Actions.
