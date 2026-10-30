"""A map drawn without any map service: points on a plain background, to scale.

Nothing is fetched from anywhere: the picture is an inline SVG built from the coordinates, so no
coordinate leaves the machine when a page is viewed. There are no roads or coastlines, only the
points, a scale bar and a north marker. Straight lines between points are labelled by the caller
as straight lines, never as routes.
"""
from __future__ import annotations

import html
import math
from typing import Sequence, Tuple

WIDTH, HEIGHT, PAD = 640, 420, 40
KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON_EQUATOR = 111.320
MIN_SPAN_KM = 0.4   # a single point, or points very close together, still get a readable scale
NICE_KM = (0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000)

# point: (lat, lon, label, kind) with kind "stop", "start" or "end"
Point = Tuple[float, float, str, str]
Line = Tuple[Tuple[float, float], Tuple[float, float]]


def _valid(lat: float, lon: float) -> bool:
    return (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            and math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180)


def render(points: Sequence[Point], lines: Sequence[Line] = ()) -> str:
    pts = [p for p in points if _valid(p[0], p[1])]
    segs = [l for l in lines if _valid(*l[0]) and _valid(*l[1])]
    coords = [(p[0], p[1]) for p in pts] + [c for l in segs for c in l]
    if not coords:
        return "<p>No positions to show.</p>"
    lat_c = (min(c[0] for c in coords) + max(c[0] for c in coords)) / 2
    lon_c = (min(c[1] for c in coords) + max(c[1] for c in coords)) / 2
    kx = KM_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat_c))

    def km(lat, lon):
        return (lon - lon_c) * kx, (lat - lat_c) * KM_PER_DEG_LAT

    xs = [km(*c)[0] for c in coords]
    ys = [km(*c)[1] for c in coords]
    span_x = max(max(xs) - min(xs), MIN_SPAN_KM)
    span_y = max(max(ys) - min(ys), MIN_SPAN_KM)
    scale = min((WIDTH - 2 * PAD) / span_x, (HEIGHT - 2 * PAD) / span_y)  # pixels per km
    mid_x, mid_y = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def px(lat, lon):
        x, y = km(lat, lon)
        return WIDTH / 2 + (x - mid_x) * scale, HEIGHT / 2 - (y - mid_y) * scale

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="100%" '
           f'role="img" style="max-width:{WIDTH}px;border:1px solid currentColor;border-radius:6px">'
           "<title>Positions drawn to scale on a plain background</title>"]
    for a, b in segs:
        (x1, y1), (x2, y2) = px(*a), px(*b)
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                   f'stroke="currentColor" stroke-width="1.5" stroke-dasharray="6 5" opacity=".55"/>')
    for lat, lon, label, kind in pts:
        x, y = px(lat, lon)
        title = f"<title>{html.escape(str(label))}</title>"
        if kind == "start":
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="none" stroke="currentColor" '
                       f'stroke-width="2.5">{title}</circle>')
        elif kind == "end":
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="currentColor">{title}</circle>')
        else:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="currentColor" opacity=".6">{title}</circle>')

    bar_km = max((k for k in NICE_KM if k * scale <= WIDTH / 4), default=NICE_KM[0])
    bar_px = bar_km * scale
    x0, y0 = 16, HEIGHT - 18
    label = f"{bar_km:g} km" if bar_km >= 1 else f"{round(bar_km * 1000)} m"
    out.append(f'<line x1="{x0}" y1="{y0}" x2="{x0 + bar_px:.1f}" y2="{y0}" stroke="currentColor" stroke-width="3"/>')
    out.append(f'<text x="{x0}" y="{y0 - 6}" font-size="12" fill="currentColor">{label}</text>')
    out.append(f'<text x="{WIDTH - 24}" y="22" font-size="14" fill="currentColor">N&#8593;</text>')
    out.append("</svg>")
    return "".join(out)
