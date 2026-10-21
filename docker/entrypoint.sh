#!/bin/sh
# Turns environment variables into mlat-client arguments.
#
# Required:
#   MLAT_SERVER    host:port of your Mode A/C-capable mlat-server
#   LAT, LON       receiver antenna position (decimal degrees)
#   ALT            antenna height, with unit: e.g. 290m or 950ft (MSL, not AGL)
#   MLAT_USER      a name for this site (shown on the server)
# Optional:
#   BEAST_HOST     readsb/ultrafeeder host (default: ultrafeeder)
#   BEAST_PORT     Beast output port (default: 30005)
#   MODEAC_EXCLUDE squawks to drop before sending (default: none - the server filters)
#   UUID           fixed UUID for this site (default: derived from MLAT_USER, LAT, LON)
#   AIRCRAFT_JSON  the local readsb's aircraft.json, for squawks it decoded from any source
#                  (default: http://$BEAST_HOST/data/aircraft.json; set empty to skip)
#   EXTRA_ARGS     anything else to pass straight to mlat-client
set -eu

fail() { echo "mlat-modeac: $1" >&2; sleep 60; exit 1; }

[ -n "${MLAT_SERVER:-}" ] || fail "MLAT_SERVER is not set"
[ -n "${LAT:-}" ] && [ -n "${LON:-}" ] || fail "LAT and LON must be set - are FEEDER_LAT / FEEDER_LONG in your .env?"
case "${ALT:-}" in
    ""|m|ft) fail "ALT must be set, with a unit (e.g. 290m or 950ft) - is FEEDER_ALT_M in your .env?" ;;
esac
[ -n "${MLAT_USER:-}" ] || fail "MLAT_USER is not set - is FEEDER_NAME in your .env?"

BEAST_HOST="${BEAST_HOST:-ultrafeeder}"
BEAST_PORT="${BEAST_PORT:-30005}"
MODEAC_EXCLUDE="${MODEAC_EXCLUDE-}"

if [ -z "${UUID:-}" ]; then
    if [ -s /data/uuid ]; then
        UUID="$(cat /data/uuid)"              # kept from an older setup that mounted /data
    else
        # stable without any storage: derived from the site's name and position, so it
        # survives container re-creation and image updates
        UUID="$(python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, "modeac.net/" + "|".join(sys.argv[1:])))' \
                "${MLAT_USER}" "${LAT}" "${LON}")"
    fi
fi

set -- \
    --input-type beast \
    --input-connect "${BEAST_HOST}:${BEAST_PORT}" \
    --server "${MLAT_SERVER}" \
    --lat "${LAT}" --lon "${LON}" --alt "${ALT}" \
    --user "${MLAT_USER}" \
    --uuid "${UUID}" \
    --forward-modeac \
    --modeac-exclude "${MODEAC_EXCLUDE}" \
    --aircraft-json "${AIRCRAFT_JSON-http://${BEAST_HOST}/data/aircraft.json}" \
    --stats-interval 300 \
    --log-timestamps

# shellcheck disable=SC2086
exec python3 /app/mlat-client "$@" ${EXTRA_ARGS:-}
