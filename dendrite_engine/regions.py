"""Generate different cut styles inside user-drawn regions of the same board."""

import numpy as np
import shapely
from shapely.geometry import Polygon, MultiPoint, LineString
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, snap

from .artwork import photo_image, sample_photo
from .solvers import CutGeometry, solve_cut
from .styles import CUT_FAMILIES


def solve_regions(
    board,
    regions,
    requested_pieces,
    seed,
    *,
    whimsies=(),
    artwork_image=None,
    **options,
):
    """Regions use normalized board coordinates. The last may take the remaining area."""
    if not isinstance(regions, list) or len(regions) < 2:
        raise ValueError("Specify at least two cut regions.")
    x0, y0, x1, y1 = board.bounds
    size = np.array([x1 - x0, y1 - y0])
    used = Polygon()
    domains = []
    for i, spec in enumerate(regions):
        if spec.get("remaining") and i == len(regions) - 1:
            poly = board.difference(used)
        else:
            points = np.asarray(spec["points"], dtype=float)
            if (
                points.ndim != 2
                or points.shape[1] != 2
                or len(points) < 3
                or not np.isfinite(points).all()
            ):
                raise ValueError(
                    "A region needs at least three finite coordinate pairs."
                )
            poly = Polygon(points * size + [x0, y0])
            if not poly.is_valid:
                raise ValueError("Region outlines must be simple polygons.")
            poly = poly.intersection(board)
        if (
            poly.geom_type != "Polygon"
            or poly.is_empty
            or poly.area < 0.01
            or used.intersection(poly).area > 0.001
        ):
            raise ValueError("Each cut region must be connected and have its own area.")
        if spec.get("style") not in CUT_FAMILIES:
            raise ValueError("Each region needs a supported cut style.")
        used = used.union(poly)
        domains.append((poly, spec))
    if (
        used.symmetric_difference(board).area > 0.001
        or sum(int(s["pieces"]) for _, s in domains) != requested_pieces
    ):
        raise ValueError(
            "Regions must cover the board and their piece counts must add up to the board total."
        )
    boundaries = [poly.boundary for poly, _ in domains]
    paths = []
    assigned = []
    region_meta = []
    photo = photo_image(artwork_image) if artwork_image is not None else None
    for index, (poly, spec) in enumerate(domains):
        figures = [w for w in whimsies if poly.contains(w.polygon)]
        assigned.extend(figures)
        count = int(spec["pieces"])
        if count < 2 + len(figures):
            raise ValueError("Allow at least two ordinary pieces per region.")
        image = None
        if photo is not None:
            px0, py0, px1, py1 = poly.bounds
            window = (px0 - x0, py0 - y0, px1 - x0, py1 - y0)
            image = sample_photo(
                photo,
                size,
                window,
                (max(8, round((px1 - px0) * 3)), max(8, round((py1 - py0) * 3))),
            )
        cut = solve_cut(
            poly,
            spec["style"],
            count,
            seed + index,
            whimsies=figures,
            artwork_image=image,
            **options,
        )
        paths.extend(cut.interior_lines)
        region_meta.append(
            {"style": spec["style"], "pieces": count, "engine": cut.metadata["engine"]}
        )
    if len(assigned) != len(whimsies):
        raise ValueError("Each fixed figure must fit inside one cut region.")
    # Split all copies of the region perimeter at the same exported seam endpoints.
    ends = MultiPoint(
        [point for line in paths for point in (line.coords[0], line.coords[-1])]
    )
    edges = shapely.union_all(
        [snap(line, ends, 0.01) for line in boundaries] + paths, grid_size=0.001
    )
    faces = [
        orient(p, 1)
        for p in polygonize(shapely.get_parts(edges))
        if p.area > 0.1 and board.covers(p.representative_point())
    ]
    if len(faces) != requested_pieces:
        raise ValueError(
            f"Region network produced {len(faces)} of {requested_pieces} pieces. Change the seed or region boundary."
        )
    # Use incidence of rounded segments, so production paths and player pieces match.
    incidence = {}
    for f in faces:
        for ring in (f.exterior, *f.interiors):
            for a, b in zip(ring.coords, list(ring.coords)[1:]):
                pair = tuple(sorted((a, b)))
                incidence[pair] = incidence.get(pair, 0) + 1

    def lines(count):
        group = [LineString(seg) for seg, n in incidence.items() if n == count]
        return list(shapely.get_parts(shapely.line_merge(shapely.union_all(group))))

    fixed = [
        next(
            i for i, f in enumerate(faces) if f.covers(w.polygon.representative_point())
        )
        for w in whimsies
    ]
    return CutGeometry(
        faces,
        lines(2),
        lines(1),
        board,
        fixed,
        {"engine": "regional", "style": "mixed", "regions": region_meta},
    )
