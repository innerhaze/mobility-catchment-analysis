"""Reproduce the catchment-analysis method on a tiny synthetic dataset.

The coordinates in this script are not a real location. The anchor point
(latitude 0.5, longitude -1.0) sits in the ocean west of the equator, which
makes it obvious that the output is illustrative rather than empirical.

What the script does
--------------------
1. Generates a synthetic set of device pings around one fictional
   establishment. Each ping has a device id, latitude, longitude and
   timestamp.
2. Detects visits by point-in-polygon containment against the
   establishment footprint, after a bounding-box pre-filter.
3. For each detected visit, extracts the device's pings in a pre-visit
   window and a post-visit window.
4. Aggregates the pre-visit pings onto a hexagonal grid (H3) and renders
   the intensity surface.
5. Computes a convex hull and a concave hull (alpha shape) over the
   same pre-visit pings for comparison.
6. Writes four static images into docs/ and an interactive HTML map at
   docs/interactive_map.html.

Dependencies
------------
numpy, pandas, shapely, h3 (v4), alphashape, folium, matplotlib.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import random

import alphashape
import folium
import h3
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Polygon as MplPolygon
from shapely.geometry import MultiPoint, Point, Polygon

# ---------------------------------------------------------------------------
# Synthetic study area. These coordinates sit in open ocean on purpose.
# ---------------------------------------------------------------------------
ANCHOR_LAT = 0.5
ANCHOR_LNG = -1.0

# A rectangular establishment footprint, roughly 60 m by 40 m at this latitude.
ESTABLISHMENT_WKT_COORDS = [
    (ANCHOR_LNG - 0.00030, ANCHOR_LAT - 0.00020),
    (ANCHOR_LNG + 0.00030, ANCHOR_LAT - 0.00020),
    (ANCHOR_LNG + 0.00030, ANCHOR_LAT + 0.00020),
    (ANCHOR_LNG - 0.00030, ANCHOR_LAT + 0.00020),
    (ANCHOR_LNG - 0.00030, ANCHOR_LAT - 0.00020),
]
ESTABLISHMENT = Polygon(ESTABLISHMENT_WKT_COORDS)

OUT_DIR = pathlib.Path(__file__).parent / "docs"
OUT_DIR.mkdir(exist_ok=True)

RNG = np.random.default_rng(42)
random.seed(42)


# ---------------------------------------------------------------------------
# 1. Generate synthetic pings
# ---------------------------------------------------------------------------
def _random_time(base: dt.datetime, hours: float) -> dt.datetime:
    return base + dt.timedelta(hours=hours)


def generate_pings(n_devices: int = 30, pings_per_device: int = 60) -> pd.DataFrame:
    """Build a tidy frame of device pings around the establishment.

    Each device has a home centre within walking or short-drive range
    of the establishment. Visits happen at specific moments and are
    preceded by a short trajectory of pings along a line from the home
    toward the establishment, so pre-visit windows produce a visible
    catchment pattern rather than scattered noise.
    """
    rows = []
    base_time = dt.datetime(2026, 1, 1, 8, 0, 0)

    for d in range(n_devices):
        device_id = f"dev-{d:03d}"
        # Devices live in a tighter radius around the anchor (roughly 1-2 km
        # at this latitude). Direction is uniform; distance biased short.
        angle = RNG.uniform(0.0, 2.0 * np.pi)
        radius = RNG.uniform(0.006, 0.014)
        home_lat = ANCHOR_LAT + radius * np.sin(angle)
        home_lng = ANCHOR_LNG + radius * np.cos(angle)

        # Each device picks 1-2 moments for a visit.
        visit_times = sorted(
            [RNG.uniform(2.0, 46.0) for _ in range(RNG.integers(1, 3))]
        )

        for i in range(pings_per_device):
            # Decide what kind of ping this is.
            draw = RNG.random()

            # 10% chance it is a visit ping (inside footprint, around one of
            # the planned visit times).
            if draw < 0.10 and visit_times:
                vt = random.choice(visit_times)
                t = _random_time(base_time, vt + RNG.normal(0.0, 0.03))
                lat = ANCHOR_LAT + RNG.normal(0.0, 0.00008)
                lng = ANCHOR_LNG + RNG.normal(0.0, 0.00012)
            # 25% chance it is a "pre-visit trajectory" ping: on the straight
            # line between home and anchor, 5 to 40 min before a visit.
            elif draw < 0.35 and visit_times:
                vt = random.choice(visit_times)
                minutes_before = RNG.uniform(5.0, 40.0)
                t = _random_time(base_time, vt - (minutes_before / 60.0))
                # fraction 0 = at home, 1 = at establishment
                fraction = 1.0 - (minutes_before / 45.0)
                lat = home_lat + (ANCHOR_LAT - home_lat) * fraction
                lng = home_lng + (ANCHOR_LNG - home_lng) * fraction
                # Light GPS noise.
                lat += RNG.normal(0.0, 0.0004)
                lng += RNG.normal(0.0, 0.0004)
            # Otherwise: ambient ping around home.
            else:
                t = _random_time(base_time, RNG.uniform(0.0, 48.0))
                lat = home_lat + RNG.normal(0.0, 0.003)
                lng = home_lng + RNG.normal(0.0, 0.003)

            rows.append((device_id, lat, lng, t))

    frame = pd.DataFrame(rows, columns=["device_id", "lat", "lng", "timestamp"])
    return frame.sort_values(["device_id", "timestamp"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. Visit detection with bounding-box pre-filter
# ---------------------------------------------------------------------------
def detect_visits(pings: pd.DataFrame, footprint: Polygon) -> pd.DataFrame:
    minx, miny, maxx, maxy = footprint.bounds
    candidate_mask = (
        (pings["lng"] >= minx)
        & (pings["lng"] <= maxx)
        & (pings["lat"] >= miny)
        & (pings["lat"] <= maxy)
    )
    candidates = pings[candidate_mask].copy()
    if candidates.empty:
        return candidates

    inside = candidates.apply(
        lambda r: footprint.contains(Point(r["lng"], r["lat"])), axis=1
    )
    return candidates[inside].copy()


# ---------------------------------------------------------------------------
# 3. Pre-visit windows
# ---------------------------------------------------------------------------
def pre_visit_pings(
    pings: pd.DataFrame, visits: pd.DataFrame, window_minutes: int = 45
) -> pd.DataFrame:
    """Return pings that fall in the window before each visit, per device."""
    pieces = []
    for _, visit in visits.iterrows():
        lower = visit["timestamp"] - dt.timedelta(minutes=window_minutes)
        upper = visit["timestamp"]
        mask = (
            (pings["device_id"] == visit["device_id"])
            & (pings["timestamp"] >= lower)
            & (pings["timestamp"] < upper)
        )
        pieces.append(pings[mask])
    if not pieces:
        return pings.iloc[0:0]
    return pd.concat(pieces, ignore_index=True)


# ---------------------------------------------------------------------------
# 4. Hex-grid aggregation
# ---------------------------------------------------------------------------
def hex_counts(pings: pd.DataFrame, resolution: int = 10) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, row in pings.iterrows():
        cell = h3.latlng_to_cell(row["lat"], row["lng"], resolution)
        counts[cell] = counts.get(cell, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 5. Hulls
# ---------------------------------------------------------------------------
def hulls(points: list[tuple[float, float]]):
    """Return (convex_hull, concave_hull_or_None) for a list of (lng, lat)."""
    convex = MultiPoint(points).convex_hull
    try:
        concave = alphashape.alphashape(points, alpha=200.0)
        if concave.is_empty or concave.geom_type not in ("Polygon", "MultiPolygon"):
            concave = None
    except Exception:
        concave = None
    return convex, concave


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _axis_window(points: list[tuple[float, float]], pad: float = 0.001):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad)


def plot_visits(pings: pd.DataFrame, visits: pd.DataFrame, footprint: Polygon):
    fig, ax = plt.subplots(figsize=(8, 7))
    ax.scatter(pings["lng"], pings["lat"], s=4, c="#999", alpha=0.4, label="pings")
    ax.scatter(
        visits["lng"], visits["lat"], s=18, c="#d81b60", label="inside footprint"
    )
    poly = MplPolygon(
        list(footprint.exterior.coords),
        fill=False,
        edgecolor="#111",
        linewidth=1.4,
    )
    ax.add_patch(poly)
    ax.set_title("Visit detection by point-in-polygon")
    ax.set_xlabel("longitude (synthetic)")
    ax.set_ylabel("latitude (synthetic)")
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="upper right")
    xmin, xmax, ymin, ymax = _axis_window(list(footprint.exterior.coords), pad=0.015)
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    fig.tight_layout()
    out = OUT_DIR / "01_visits.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_hex_grid(counts: dict[str, int]):
    fig, ax = plt.subplots(figsize=(8, 7))
    all_cells = list(counts.keys())
    if not all_cells:
        ax.text(0.5, 0.5, "no pre-visit pings", ha="center", va="center")
        out = OUT_DIR / "02_hex_grid.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        return out

    max_count = max(counts.values())
    cmap = plt.get_cmap("viridis")
    for cell, count in counts.items():
        boundary = h3.cell_to_boundary(cell)
        lngs = [b[1] for b in boundary]
        lats = [b[0] for b in boundary]
        colour = cmap(count / max_count)
        ax.fill(lngs, lats, color=colour, alpha=0.75, edgecolor="#111", linewidth=0.3)

    ax.set_title("Pre-visit pings aggregated on H3 grid (resolution 10)")
    ax.set_xlabel("longitude (synthetic)")
    ax.set_ylabel("latitude (synthetic)")
    ax.set_aspect("equal", adjustable="box")
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(vmin=0, vmax=max_count))
    plt.colorbar(sm, ax=ax, label="ping count")
    fig.tight_layout()
    out = OUT_DIR / "02_hex_grid.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_hulls(points, convex, concave, footprint):
    fig, ax = plt.subplots(figsize=(8, 7))
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ax.scatter(xs, ys, s=6, c="#555", alpha=0.5, label="pre-visit pings")

    convex_coords = list(convex.exterior.coords)
    ax.plot(
        [c[0] for c in convex_coords],
        [c[1] for c in convex_coords],
        color="#1e88e5",
        linewidth=2.0,
        label="convex hull",
    )

    if concave is not None:
        polys = (
            [concave] if concave.geom_type == "Polygon" else list(concave.geoms)
        )
        first = True
        for p in polys:
            cc = list(p.exterior.coords)
            ax.plot(
                [c[0] for c in cc],
                [c[1] for c in cc],
                color="#d81b60",
                linewidth=2.0,
                label="concave hull" if first else None,
            )
            first = False

    poly = MplPolygon(
        list(footprint.exterior.coords),
        fill=False,
        edgecolor="#111",
        linewidth=1.2,
        linestyle="--",
    )
    ax.add_patch(poly)

    ax.set_title("Convex hull vs concave hull on the same point cloud")
    ax.set_xlabel("longitude (synthetic)")
    ax.set_ylabel("latitude (synthetic)")
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="upper right")
    fig.tight_layout()
    out = OUT_DIR / "03_hulls.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def write_interactive_map(pings, visits, pre_pings, convex, concave, footprint):
    centre = [ANCHOR_LAT, ANCHOR_LNG]
    m = folium.Map(location=centre, zoom_start=15, tiles="CartoDB positron")

    footprint_coords = [(lat, lng) for lng, lat in footprint.exterior.coords]
    folium.Polygon(
        footprint_coords,
        color="#111",
        weight=2,
        fill=False,
        tooltip="establishment footprint",
    ).add_to(m)

    for _, row in pre_pings.iterrows():
        folium.CircleMarker(
            location=(row["lat"], row["lng"]),
            radius=2,
            color="#555",
            opacity=0.5,
        ).add_to(m)

    for _, row in visits.iterrows():
        folium.CircleMarker(
            location=(row["lat"], row["lng"]),
            radius=4,
            color="#d81b60",
            fill=True,
            fill_opacity=0.8,
            tooltip=f"visit at {row['timestamp']}",
        ).add_to(m)

    convex_coords = [(lat, lng) for lng, lat in convex.exterior.coords]
    folium.Polygon(
        convex_coords,
        color="#1e88e5",
        weight=2,
        fill=False,
        tooltip="convex hull",
    ).add_to(m)

    if concave is not None and concave.geom_type == "Polygon":
        concave_coords = [(lat, lng) for lng, lat in concave.exterior.coords]
        folium.Polygon(
            concave_coords,
            color="#d81b60",
            weight=2,
            fill=False,
            tooltip="concave hull",
        ).add_to(m)

    out = OUT_DIR / "interactive_map.html"
    m.save(str(out))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    print("Generating synthetic pings...")
    pings = generate_pings()
    print(f"  pings: {len(pings)}")

    print("Detecting visits by point-in-polygon...")
    visits = detect_visits(pings, ESTABLISHMENT)
    print(f"  visits inside footprint: {len(visits)}")

    print("Extracting pre-visit windows (45 min)...")
    pre = pre_visit_pings(pings, visits, window_minutes=45)
    print(f"  pre-visit pings: {len(pre)}")

    print("Aggregating onto H3 grid (resolution 10)...")
    counts = hex_counts(pre, resolution=10)
    print(f"  hex cells touched: {len(counts)}")

    print("Computing convex and concave hulls...")
    pts = list(zip(pre["lng"].tolist(), pre["lat"].tolist()))
    convex, concave = hulls(pts)
    print(f"  convex ok: {convex is not None}  concave ok: {concave is not None}")

    print("Rendering images...")
    plot_visits(pings, visits, ESTABLISHMENT)
    plot_hex_grid(counts)
    plot_hulls(pts, convex, concave, ESTABLISHMENT)
    write_interactive_map(pings, visits, pre, convex, concave, ESTABLISHMENT)

    print("Done. Outputs in docs/")


if __name__ == "__main__":
    main()
