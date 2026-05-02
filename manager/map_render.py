"""Tile-pyramid compositing for Discord image attachments.

Stitches PNG tiles from data/map/<level>/<z>/<x>/<y>.png into a
single image, optionally drawing a player marker at a given UE
world coordinate. Returns PNG bytes ready to attach to a Discord
webhook POST.

Coord transform constants come from memory/coordinate_transform.md
(verified against 6 calibration landmarks: 1px = 50 UE units at
zoom 6, world centre = pixel (8160, 8160)). Both maps share these
constants until per-level recalibration is needed.

This module imports Pillow lazily inside each function so the rest
of the manager keeps booting even if Pillow isn't installed (e.g.
on a fresh server that hasn't run pip install yet).
"""

import io
import logging
from pathlib import Path
from typing import Optional

from manager.config import PROJECT_ROOT

log = logging.getLogger(__name__)

# Tile pyramid metadata (matches what's in data/map/<level>/<z>/<x>/<y>.png)
TILE_PX = 255  # actual on-disk size; gamingwithdaopa says 256 but our tiles are 255
WORLD_CENTRE_PIXEL_AT_Z6 = 8160  # UE (0,0) lands here on the zoom-6 grid
UE_PER_PIXEL_AT_Z6 = 50.0
TILE_DIR = PROJECT_ROOT / "data" / "map"

# Default zoom for "where did this player log in?" snapshots.
#
# Zoom 5 = 32x32 tiles total. A 3x3 viewport at z5 = 765x765 px,
# covering 3/32 = ~9% of map width = ~0.9% of the map's surface
# area. Tight enough that the player's immediate surroundings
# dominate the frame (terrain, nearby POIs / pyramids / camps
# visible) without flooding the channel with a giant image.
#
# Earlier versions used z4 + 5x5 viewport (1275x1275, ~31% width
# = ~9.7% of map area) which was too pulled-back -- you couldn't
# tell where the player actually was relative to features they'd
# care about. Operator feedback April 2026.
#
# Both viewport_tiles values must stay ODD so the viewport
# centres on a single tile rather than the seam between two.
DEFAULT_ZOOM = 5
DEFAULT_VIEWPORT_TILES = 3

# Whole-map overview default (for first-time players with no
# last-known location). Zoom 2 = 4x4 = 1020x1020 px.
OVERVIEW_ZOOM = 2

# Discord caps webhook attachments at 8 MB unless boosted. PNG of a
# tile-stitched 1275x1275 image is typically ~500 KB to 1 MB.


def world_to_tile_pixel(pos_x: float, pos_y: float,
                         zoom: int = 6) -> tuple[int, int]:
    """UE world coord -> global pixel at the named zoom level.
    See memory/coordinate_transform.md for derivation."""
    scale = (1.0 / UE_PER_PIXEL_AT_Z6) * (2 ** (zoom - 6))
    centre = WORLD_CENTRE_PIXEL_AT_Z6 * (2 ** (zoom - 6))
    return (int(scale * pos_x + centre), int(scale * pos_y + centre))


def _level_tile_dir(level: str, zoom: int) -> Path:
    return TILE_DIR / level / str(zoom)


def _open_tile(path: Path):
    """Lazy Pillow import + open. Returns None if file missing."""
    if not path.exists():
        return None
    from PIL import Image
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        log.exception("map_render: failed to open tile %s", path)
        return None


def composite_player_view(level: str, pos_x: float, pos_y: float, *,
                           zoom: int = DEFAULT_ZOOM,
                           viewport_tiles: int = DEFAULT_VIEWPORT_TILES,
                           marker_label: Optional[str] = None
                           ) -> Optional[bytes]:
    """Render a viewport_tiles x viewport_tiles tile composite centred
    on the UE coord (pos_x, pos_y), with a marker dot + optional text
    label. Returns PNG bytes, or None if the tile pyramid for `level`
    isn't on disk.

    If the marker falls inside a tile we don't have, we still composite
    whatever tiles ARE present and skip the missing ones (black). If
    the entire tile dir is missing, returns None so the caller can
    fall back to a different image."""
    from PIL import Image, ImageDraw, ImageFont

    tile_dir = _level_tile_dir(level, zoom)
    if not tile_dir.exists():
        log.warning("map_render: no tile dir for level=%s zoom=%d (%s)",
                    level, zoom, tile_dir)
        return None

    # Pixel-of-marker in the global zoom-N grid
    px, py = world_to_tile_pixel(pos_x, pos_y, zoom=zoom)
    # Centre tile coords
    cx = px // TILE_PX
    cy = py // TILE_PX
    half = viewport_tiles // 2

    canvas_size = viewport_tiles * TILE_PX
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (10, 10, 10, 255))

    pasted = 0
    for ty_offset in range(-half, half + 1):
        for tx_offset in range(-half, half + 1):
            tx = cx + tx_offset
            ty = cy + ty_offset
            tile = _open_tile(tile_dir / str(tx) / f"{ty}.png")
            if tile is not None:
                paste_x = (tx_offset + half) * TILE_PX
                paste_y = (ty_offset + half) * TILE_PX
                canvas.paste(tile, (paste_x, paste_y))
                pasted += 1

    if pasted == 0:
        log.warning("map_render: no tiles found around (cx=%d, cy=%d) "
                    "at level=%s zoom=%d", cx, cy, level, zoom)
        return None

    # Marker position within the canvas
    marker_x = px - (cx - half) * TILE_PX
    marker_y = py - (cy - half) * TILE_PX

    draw = ImageDraw.Draw(canvas)
    # Outer ring (visibility against varied terrain)
    r_outer = 14
    draw.ellipse([marker_x - r_outer, marker_y - r_outer,
                  marker_x + r_outer, marker_y + r_outer],
                 outline="#ff3333", width=4)
    # Inner solid dot
    r_inner = 5
    draw.ellipse([marker_x - r_inner, marker_y - r_inner,
                  marker_x + r_inner, marker_y + r_inner],
                 fill="#ff3333")

    if marker_label:
        try:
            # Pillow 10+ ImageFont.load_default() returns a real font
            font = ImageFont.load_default()
        except Exception:
            font = None
        # Text with a thin black outline so it's readable on any
        # terrain colour
        text_x, text_y = marker_x + 18, marker_y - 8
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((text_x + dx, text_y + dy), marker_label,
                          fill="black", font=font)
        draw.text((text_x, text_y), marker_label,
                  fill="white", font=font)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def composite_overview(level: str, *, zoom: int = OVERVIEW_ZOOM
                       ) -> Optional[bytes]:
    """Whole-map overview at the given zoom. For first-time players or
    when the player's coords are unknown. Returns PNG bytes or None
    if the tile dir is missing."""
    from PIL import Image

    tile_dir = _level_tile_dir(level, zoom)
    if not tile_dir.exists():
        log.warning("map_render: no tile dir for overview level=%s zoom=%d",
                    level, zoom)
        return None

    n = 2 ** zoom  # tiles per side at this zoom
    canvas_size = n * TILE_PX
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (10, 10, 10, 255))
    pasted = 0
    for tx in range(n):
        for ty in range(n):
            tile = _open_tile(tile_dir / str(tx) / f"{ty}.png")
            if tile is not None:
                canvas.paste(tile, (tx * TILE_PX, ty * TILE_PX))
                pasted += 1

    if pasted == 0:
        log.warning("map_render: zero tiles found for overview "
                    "level=%s zoom=%d", level, zoom)
        return None

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()
