"""Registered duplex and spherical artwork for the Infinity generator."""

import base64
import math
from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw, ImageOps


def _sphere_points(weights, corners):
    """Invert spherical areal barycentrics using the solid-angle formula.

    Each target subtriangle area yields a plane equation for its unknown
    unit-sphere vertex. Solve the three equations together for each pixel.
    """
    b, c = np.roll(corners, -1, axis=0), np.roll(corners, -2, axis=0)
    normals = np.cross(b, c)
    det = np.dot(corners[0], normals[0])
    dots = (b * c).sum(axis=1)
    area = 2 * math.atan2(det, 1 + sum(dots))
    tangent = np.tan(weights * (area / 2))
    plane = normals[None, :, :] - tangent[:, :, None] * (b + c)
    offset = tangent * (1 + dots)
    xyz = np.linalg.solve(plane, offset[:, :, None])[:, :, 0]
    return xyz / np.linalg.norm(xyz, axis=1)[:, None]


def orient_map_vertices(xyz, mask_file, seed):
    """Search globe rotations for one that keeps projection corners off marked land."""
    from PIL import Image
    from scipy.spatial.transform import Rotation

    with Image.open(mask_file) as image:
        mask = np.asarray(image.convert("L")) >= 128
    h, w = mask.shape
    if h < 2 or w < 2:
        raise ValueError("The land mask must be an equirectangular image.")
    rng = np.random.default_rng(seed)
    rotations = Rotation.random(4095, random_state=rng).as_matrix()
    rotations = np.concatenate(([np.eye(3)], rotations))
    candidates = np.einsum("rij,vj->rvi", rotations, xyz)
    u = (np.arctan2(candidates[:, :, 1], candidates[:, :, 0]) / (2 * np.pi) + 0.5) % 1
    v = 0.5 - np.arcsin(np.clip(candidates[:, :, 2], -1, 1)) / np.pi
    land = mask[np.clip((v * h).astype(int), 0, h - 1), (u * w).astype(int) % w].sum(
        axis=1
    )
    selected = int(land.argmin())
    return candidates[selected], {
        "original_vertices_on_land": int(land[0]),
        "selected_vertices_on_land": int(land[selected]),
        "rotations_evaluated": len(rotations),
        "rotation_matrix": rotations[selected].tolist(),
    }


def _sphere_uv(points, chart_index, charts, xyz):
    ids, corners = charts[chart_index]
    basis = corners[1:] - corners[0]
    ab = (points - corners[0]) @ np.linalg.inv(basis)
    weights = np.column_stack((1 - ab.sum(axis=1), ab))
    exits = weights.argmin(axis=1)
    exits[weights.min(axis=1) >= -1e-10] = -1
    out = np.empty((len(points), 3))
    for side in np.unique(exits):
        where = exits == side
        area_weights = weights[where].copy()
        face_ids = list(ids)
        if side != -1:
            # Project a tab that extends past the chart in the neighboring
            # triangle. Reflect the third vertex in the shared equilateral edge.
            shared = set(ids) - {ids[side]}
            neighbor = next(
                other for other, _ in charts if other != ids and shared <= set(other)
            )
            face_ids[side] = (set(neighbor) - shared).pop()
            t = area_weights[:, side].copy()
            area_weights += t[:, None]
            area_weights[:, side] = -t
        area_weights = np.maximum(area_weights, 0)
        area_weights /= area_weights.sum(axis=1)[:, None]
        out[where] = _sphere_points(area_weights, xyz[face_ids])
    lon = np.arctan2(out[:, 1], out[:, 0])
    lat = np.arcsin(np.clip(out[:, 2], -1, 1))
    return (lon / (2 * math.pi) + 0.5) % 1, 0.5 - lat / math.pi


def _planar_uv(points, periods, klein, back):
    x, y = points.T / np.asarray(periods)[:, None]
    if klein:
        # Second sheet of the orientable double cover.
        if back:
            x, y = x + 1, 1 - y
        x = x / 2
    return x % 1, y % 1


def _projective_uv(points, periods, back):
    """Map the projective plane's two print sides onto its spherical double cover."""
    x, y = (2 * points / periods - 1).T.copy()
    reverse = np.full(len(x), back)
    # Fold protruding tabs into their partner chart before looking up the art.
    outside = np.abs(x) > 1
    x[outside] -= 2 * np.sign(x[outside])
    y[outside] *= -1
    reverse ^= outside
    outside = np.abs(y) > 1
    y[outside] -= 2 * np.sign(y[outside])
    x[outside] *= -1
    reverse ^= outside
    # Square-to-disk followed by the upper hemisphere. Opposite positions on
    # the rim then coincide on the opposite sheet under the antipodal map.
    a = x * np.sqrt(np.maximum(0, 1 - y * y / 2))
    b = y * np.sqrt(np.maximum(0, 1 - x * x / 2))
    c = np.sqrt(np.maximum(0, (1 - x * x) * (1 - y * y)))
    sign = np.where(reverse, -1, 1)
    return (np.arctan2(sign * b, sign * a) / (2 * math.pi) + 0.5) % 1, 0.5 - np.arcsin(
        np.clip(sign * c, -1, 1)
    ) / math.pi


def _sample(source, u, v, sphere, seed):
    if source is None:
        # An original continuous, periodic atlas field with a coordinate grid.
        # Fade longitude-dependence to zero at the two geographic poles.
        p = math.radians(seed % 360)
        pole = np.sin(math.pi * v) if sphere else 1.0
        wave = np.cos(2 * math.pi * u + p) * pole
        field = np.clip(
            0.5 + 0.28 * wave + 0.20 * np.cos(2 * math.pi * v - 0.5 * wave), 0, 1
        )
        low, mid, high = (
            np.array([17, 47, 86]),
            np.array([50, 154, 153]),
            np.array([254, 217, 151]),
        )
        rgb = np.where(
            field[:, None] < 0.5,
            low + (mid - low) * 2 * field[:, None],
            mid + (high - mid) * (2 * field[:, None] - 1),
        )
        grid = ((np.abs((u * 12 + 0.5) % 1 - 0.5) < 0.014) & (pole > 0.12)) | (
            np.abs((v * 8 + 0.5) % 1 - 0.5) < 0.012
        )
        rgb[grid] = 0.65 * rgb[grid] + 0.35 * 255
        return rgb.astype(np.uint8)
    h, w = source.shape[:2]
    x = (u * w - 0.5) % w
    y = np.clip(v * h - 0.5, 0, h - 1) if sphere else (v * h - 0.5) % h
    ix, iy = x.astype(int), y.astype(int)
    fx, fy = (x - ix)[:, None], (y - iy)[:, None]
    ix2, iy2 = (ix + 1) % w, np.minimum(iy + 1, h - 1) if sphere else (iy + 1) % h
    return (
        (1 - fy) * ((1 - fx) * source[iy, ix] + fx * source[iy, ix2])
        + fy * ((1 - fx) * source[iy2, ix] + fx * source[iy2, ix2])
    ).astype(np.uint8)


def print_artwork(
    out, svg, pieces, bounds, topology, charts, xyz, periods, artwork, seed, density
):
    source = None
    if artwork:
        with Image.open(artwork) as image:
            source = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
    x0, y0, x1, y1 = bounds
    size = (
        max(1, math.ceil((x1 - x0) * density)),
        max(1, math.ceil((y1 - y0) * density)),
    )
    if size[0] * size[1] > 30_000_000:
        raise ValueError("Artwork exceeds 30 megapixels; lower --pixels-per-mm")
    scales = np.array(size) / np.array([x1 - x0, y1 - y0])
    mask_image = Image.new("I", size, 0)
    draw = ImageDraw.Draw(mask_image)
    for piece in pieces:
        pixels = (np.asarray(piece["outline"]) - [x0, y0]) * scales - 0.5
        draw.polygon([tuple(p) for p in pixels], fill=piece["chart"] + 1)
    mask = np.asarray(mask_image)
    for back in [False, True] if topology in ("klein", "projective") else [False]:
        pixels = np.zeros((size[1], size[0], 4), dtype=np.uint8)
        for ci in range(len(charts) or 1):
            iy, ix = np.where(mask == ci + 1)
            # Bound peak memory for spherical per-pixel matrix solves.
            for start in range(0, len(ix), 32768):
                ys, xs = iy[start : start + 32768], ix[start : start + 32768]
                points = (np.column_stack((xs, ys)) + 0.5) / scales + [x0, y0]
                if topology == "sphere":
                    u, v = _sphere_uv(points, ci, charts, xyz)
                elif topology == "projective":
                    u, v = _projective_uv(points, periods, back)
                else:
                    u, v = _planar_uv(points, periods, topology == "klein", back)
                pixels[ys, xs, :3] = _sample(
                    source, u, v, topology in ("sphere", "projective"), seed
                )
                pixels[ys, xs, 3] = 255
        image = Image.fromarray(pixels)
        if back:
            image = ImageOps.mirror(image)
        buf = BytesIO()
        image.save(buf, format="PNG")
        data = base64.b64encode(buf.getvalue()).decode("ascii")
        name = "artwork-back.svg" if back else "artwork.svg"
        mark = (
            f'<image x="{x0:.4f}" y="{y0:.4f}" width="{x1 - x0:.4f}" height="{y1 - y0:.4f}" '
            f'preserveAspectRatio="none" href="data:image/png;base64,{data}"/>'
        )
        (out / name).write_text(f"{svg}\n{mark}\n</svg>", encoding="utf-8")
