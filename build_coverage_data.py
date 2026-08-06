#!/usr/bin/env python3
"""Build the static geometry the coverage map draws.

Produces two files in data/:

    states.geojson        51 states + DC, simplified. Drawn below z6.
    jurisdictions.pmtiles 33k jurisdiction polygons, z5-z11. Drawn above z6.

Only geometry and join keys are baked in. Published status, counts and dates
all come from https://api.zoningatlas.org/atlas_jurisdictions at runtime, so
this only needs rerunning when boundaries change.

Requires the DB credentials in .env (VPN) and tippecanoe on PATH.
"""

import json
import os
import subprocess
import sys
import tempfile

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "data")

# Simplification tolerances, in metres of Web Mercator.
# Jurisdictions: 20m is far below z11's ~76 m/px, so tiles stay crisp at max zoom.
# States: only drawn below z6 (~2.4 km/px), where 500m is invisible.
JURISDICTION_TOLERANCE = 20
STATE_TOLERANCE = 500

MIN_ZOOM = 5  # one zoom of overlap with the state layer for the crossfade
MAX_ZOOM = 11


def load_env():
    env = {}
    with open(os.path.join(HERE, ".env")) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key] = value.strip().strip('"').strip("'")
    return env


def connect():
    env = load_env()
    return psycopg2.connect(
        host=env["DB_HOST"],
        port=env["DB_PORT"],
        dbname=env["DBNAME"],
        user=env["DB_USER"],
        password=env["DB_PASSWORD"],
        sslmode="require",
        connect_timeout=30,
    )


STATE_SQL = """
    select id, name, postcode, fips,
           st_asgeojson(st_transform(st_simplifypreservetopology(geom_merc, %s), 4326), 5)
    from website_state
    order by name
"""

# Every jurisdiction reaches a state through its county (verified: zero orphans).
JURISDICTION_SQL = """
    select j.id, j.name, c.state_id, s.postcode,
           st_asgeojson(st_transform(st_simplifypreservetopology(j.geom_merc, %s), 4326), 5)
    from website_jurisdiction j
    join website_county c on c.id = j.county_id
    join website_state s on s.id = c.state_id
    where j.geom_merc is not null
    order by j.id
"""


def unwrap_antimeridian(geometry):
    """Alaska's Aleutians sit at positive longitudes, which would draw the state
    smeared across the whole map. Shift them west of -180 so it stays contiguous."""

    def walk(coords):
        if isinstance(coords[0], (int, float)):
            if coords[0] > 0:
                coords[0] -= 360
            return
        for c in coords:
            walk(c)

    walk(geometry["coordinates"])
    return geometry


def build_states(conn):
    path = os.path.join(OUT_DIR, "states.geojson")
    features = []
    with conn.cursor("states") as cur:
        cur.itersize = 100
        cur.execute(STATE_SQL, (STATE_TOLERANCE,))
        for sid, name, postcode, fips, geom in cur:
            geometry = json.loads(geom)
            if name == "Alaska":
                geometry = unwrap_antimeridian(geometry)
            features.append(
                {
                    "type": "Feature",
                    "id": sid,
                    "properties": {
                        "id": sid,
                        "name": name,
                        "postcode": postcode,
                        "fips": fips,
                    },
                    "geometry": geometry,
                }
            )
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)
    print(f"  states.geojson: {len(features)} features, {os.path.getsize(path) / 1e6:.1f} MB")


def dump_jurisdictions(conn, path):
    """Stream to newline-delimited GeoJSON, which is what tippecanoe wants."""
    count = 0
    with conn.cursor("jurisdictions") as cur, open(path, "w") as fh:
        cur.itersize = 500
        cur.execute(JURISDICTION_SQL, (JURISDICTION_TOLERANCE,))
        for jid, name, state_id, postcode, geom in cur:
            fh.write(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": {
                            "id": jid,
                            "name": name,
                            "state_id": state_id,
                            "st": postcode,
                        },
                        "geometry": json.loads(geom),
                    },
                    separators=(",", ":"),
                )
            )
            fh.write("\n")
            count += 1
            if count % 5000 == 0:
                print(f"    ...{count} jurisdictions")
    return count


def build_jurisdictions(conn):
    out = os.path.join(OUT_DIR, "jurisdictions.pmtiles")
    fd, tmp = tempfile.mkstemp(suffix=".geojsonl")
    os.close(fd)
    try:
        count = dump_jurisdictions(conn, tmp)
        print(f"  streamed {count} jurisdictions ({os.path.getsize(tmp) / 1e6:.0f} MB), tiling...")
        subprocess.run(
            [
                "tippecanoe",
                "-o", out,
                "--force",
                "--layer=jurisdictions",
                f"--minimum-zoom={MIN_ZOOM}",
                f"--maximum-zoom={MAX_ZOOM}",
                # keep every feature clickable: no coalescing, no dropping by zoom
                "--no-feature-limit",
                "--no-tile-size-limit",
                "--simplification=4",
                "--detect-shared-borders",
                tmp,
            ],
            check=True,
        )
        print(f"  jurisdictions.pmtiles: {os.path.getsize(out) / 1e6:.0f} MB")
    finally:
        os.unlink(tmp)


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = connect()
    if only in (None, "states"):
        print("Building states...")
        build_states(conn)
    if only in (None, "jurisdictions"):
        print("Building jurisdictions...")
        build_jurisdictions(conn)
    conn.close()
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
