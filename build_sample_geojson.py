#!/usr/bin/env python3
"""Slim the Lafayette sample geojson used by home-map.html.

The raw exports carry every variable (184 slices / 194 districts) at full
resolution, which is ~22MB across the two files. The home map draws them in a
580px box and reads seven fields, so this keeps only what's used and simplifies
the geometry. The full variable counts are printed so they can be baked into the
popup copy, since they're no longer derivable from the trimmed properties.

    /opt/homebrew/Caskroom/miniconda/base/envs/geo/bin/python build_sample_geojson.py
"""

import json
import os

from shapely.geometry import mapping, shape

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "data")

# ~1.1e-5 degrees is roughly 1m at this latitude. The map tops out at z19
# (~0.3 m/px), so 2m keeps edges clean even fully zoomed in.
TOLERANCE = 0.00002

# Coordinate decimals to keep. 6dp is ~0.1m — more than the tolerance needs.
PRECISION = 6

SHARED_FIELDS = [
    "type",
    "acres",
    "family1_treatment",
    "family2_treatment",
    "family3_treatment",
    "family4_treatment",
]

SOURCES = {
    "slices": {
        "src": "lafayette_co_slices_sample.geojson",
        "out": "lafayette_slices.geojson",
        "fields": SHARED_FIELDS + ["slice_id"],
    },
    "districts": {
        "src": "lafayette_co_districts_sample.geojson",
        "out": "lafayette_districts.geojson",
        "fields": SHARED_FIELDS + ["name"],
    },
}


def round_coords(obj, nd):
    if isinstance(obj, (int, float)):
        return round(obj, nd)
    if isinstance(obj, (list, tuple)):
        return [round_coords(x, nd) for x in obj]
    return obj


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for key, cfg in SOURCES.items():
        src = os.path.join(HERE, cfg["src"])
        out = os.path.join(OUT_DIR, cfg["out"])
        data = json.load(open(src))

        total_vars = len(data["features"][0]["properties"])
        kept = []
        for f in data["features"]:
            geom = shape(f["geometry"]).simplify(TOLERANCE, preserve_topology=True)
            if geom.is_empty:
                continue
            g = mapping(geom)
            g["coordinates"] = round_coords(g["coordinates"], PRECISION)
            kept.append(
                {
                    "type": "Feature",
                    "properties": {k: f["properties"].get(k) for k in cfg["fields"]},
                    "geometry": g,
                }
            )

        with open(out, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": kept}, fh, separators=(",", ":"))

        before = os.path.getsize(src) / 1e6
        after = os.path.getsize(out) / 1e6
        print(
            f"{key:10} {len(kept):3} features  "
            f"{before:6.1f} MB -> {after:5.2f} MB  ({after / before * 100:.0f}%)  "
            f"| all-variable count for the popup: {total_vars}"
        )


if __name__ == "__main__":
    main()
