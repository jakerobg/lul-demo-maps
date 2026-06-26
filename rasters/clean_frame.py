#!/usr/bin/env python
"""Remove the diagonal warp-fill frame from a georeferenced thematic raster
and write a web-ready single-band COG.

Why this exists: georeferencing (gdalwarp / QGIS Georeferencer) rotates the
image to fit the control points, and fills the leftover rotated corners with
value 0 -- which collides with the "protected" class -- producing a diagonal
frame. The real data is the warped source rectangle (a convex quad); the 255
background densely fills its corners, so the convex hull of all non-fill pixels
is the true data footprint. Any 0 pixel outside that hull is frame -> nodata.

Usage:
    conda activate geo
    python clean_frame.py <in.tif> <out_cog.tif> [fill_value=0] [nodata=255]

Prefer prevention: set nodata before georeferencing so corners fill with 255:
    gdal_edit.py -a_nodata 255 source.tif
"""
import sys
import numpy as np
import rasterio
from skimage.morphology import convex_hull_image


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, out = sys.argv[1], sys.argv[2]
    fill = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    nodata = int(sys.argv[4]) if len(sys.argv) > 4 else 255

    with rasterio.open(src) as r:
        a = r.read(1)
        prof = r.profile
        crs, tr = r.crs, r.transform

    hull = convex_hull_image(a != nodata)   # data footprint, robust to connectivity
    frame = (a == fill) & ~hull             # fill-value pixels outside the footprint
    removed = int(frame.sum())
    a[frame] = nodata

    prof.update(driver="COG", dtype="uint8", count=1, nodata=nodata,
                compress="DEFLATE", crs=crs, transform=tr)
    for k in ["blockxsize", "blockysize", "tiled", "interleave", "photometric"]:
        prof.pop(k, None)
    with rasterio.open(out, "w", **prof) as dst:
        dst.write(a, 1)
    print(f"removed {removed} frame px -> {out}")


if __name__ == "__main__":
    main()
