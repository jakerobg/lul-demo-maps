#!/usr/bin/env python3
"""Build the static geometry the coverage map draws.

Produces four files in data/:

    states.geojson             51 states + DC, simplified. Drawn below z6.
    jurisdictions.pmtiles      33k jurisdiction polygons, z5-z11. Drawn above z6.
    jurisdiction_labels.pmtiles One label anchor per jurisdiction, z8+.
    jurisdiction_bounds.json   id -> [w, s, e, n], for the panel's zoom-to controls.

Only geometry and join keys are baked in. Published status, has_zoning, counts
and dates all come from https://api.zoningatlas.org/atlas_jurisdictions at
runtime, so this only needs rerunning when boundaries change.

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


# One anchor per jurisdiction. Without this MapLibre labels every part of a
# MultiPolygon separately, and 17.7% of jurisdictions are multi-part (Jefferson
# County alone has 1,078 pieces). PointOnSurface rather than Centroid so the
# anchor is guaranteed to land inside an irregular shape.
LABEL_SQL = """
    select j.id, j.name, s.postcode,
           st_asgeojson(st_transform(st_pointonsurface(big.geom), 4326), 5)
    from website_jurisdiction j
    join website_county c on c.id = j.county_id
    join website_state s on s.id = c.state_id
    cross join lateral (
        select (d).geom as geom
        from st_dump(j.geom_merc) d
        order by st_area((d).geom) desc
        limit 1
    ) big
    where j.geom_merc is not null
"""

# Labels only render from LABEL_MIN_ZOOM up, so don't tile them below that.
LABEL_MIN_ZOOM = 8


def api_names():
    """id -> display name, from the atlas_jurisdictions snapshot.

    The snapshot is what the coverage map itself reads, so labels have to agree
    with it or a jurisdiction is named one way on the map and another in the
    panel. It also carries naming the database doesn't, e.g. the
    "(Unincorporated)" suffix on county remainders.
    """
    path = os.path.join(OUT_DIR, "atlas_jurisdictions.json")
    if not os.path.exists(path):
        print(f"  no {os.path.basename(path)}; falling back to database names")
        return {}
    with open(path) as fh:
        return {r["id"]: r["name"] for r in json.load(fh) if r.get("name")}


def build_labels(conn):
    out = os.path.join(OUT_DIR, "jurisdiction_labels.pmtiles")
    names = api_names()
    fd, tmp = tempfile.mkstemp(suffix=".geojsonl")
    os.close(fd)
    try:
        count = 0
        renamed = 0
        with conn.cursor("labels") as cur, open(tmp, "w") as fh:
            cur.itersize = 1000
            cur.execute(LABEL_SQL)
            for jid, name, postcode, geom in cur:
                label = names.get(jid, name)
                if label != name:
                    renamed += 1
                fh.write(
                    json.dumps(
                        {
                            "type": "Feature",
                            "properties": {"id": jid, "name": label, "st": postcode},
                            "geometry": json.loads(geom),
                        },
                        separators=(",", ":"),
                    )
                )
                fh.write("\n")
                count += 1
        print(f"  streamed {count} label points ({renamed} named from the snapshot), tiling...")
        subprocess.run(
            [
                "tippecanoe",
                "-o", out,
                "--force",
                "--layer=jurisdiction_labels",
                f"--minimum-zoom={LABEL_MIN_ZOOM}",
                f"--maximum-zoom={MAX_ZOOM}",
                "--no-feature-limit",
                "--no-tile-size-limit",
                tmp,
            ],
            check=True,
        )
        print(f"  jurisdiction_labels.pmtiles: {os.path.getsize(out) / 1e6:.1f} MB")
    finally:
        os.unlink(tmp)


# Extent per jurisdiction, so the coverage panel can fly to one by name. Tiles
# can't answer this: a jurisdiction the user picks is usually off-screen, and
# tile geometry is clipped anyway.
# ShiftLongitude puts every vertex in 0-360 before the envelope is taken. Without
# it the Aleutians, which straddle the antimeridian, come back as a box spanning
# the entire globe (-179 to +179) and a fitBounds on it zooms out to the world.
BOUNDS_SQL = """
    select j.id, st_xmin(e), st_ymin(e), st_xmax(e), st_ymax(e)
    from website_jurisdiction j
    cross join lateral (
        select st_envelope(st_shiftlongitude(st_transform(j.geom_merc, 4326))) as e
    ) env
    where j.geom_merc is not null
    order by j.id
"""


def build_bounds(conn):
    path = os.path.join(OUT_DIR, "jurisdiction_bounds.json")
    bounds = {}
    with conn.cursor("bounds") as cur:
        cur.itersize = 2000
        cur.execute(BOUNDS_SQL)
        for jid, w, s, e, n in cur:
            # Undo the shift as a whole box, so the Americas come back negative and
            # an antimeridian crosser stays contiguous (e.g. -180.2 to -179.1)
            # rather than wrapping the long way round. MapLibre is fine past -180.
            if e > 180:
                w -= 360
                e -= 360
            # ~11 m of precision, which is well inside a fitBounds padding
            bounds[jid] = [round(w, 4), round(s, 4), round(e, 4), round(n, 4)]
    with open(path, "w") as fh:
        json.dump(bounds, fh, separators=(",", ":"))
    print(f"  jurisdiction_bounds.json: {len(bounds)} boxes, {os.path.getsize(path) / 1e6:.1f} MB")


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
    if only in (None, "jurisdictions", "labels"):
        print("Building jurisdiction labels...")
        build_labels(conn)
    if only in (None, "jurisdictions", "bounds"):
        print("Building jurisdiction bounds...")
        build_bounds(conn)
    conn.close()
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
