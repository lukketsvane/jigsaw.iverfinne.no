"""A gecko tile for the p3 wallpaper group.

Three junctions lie at the corners of an equilateral triangle. Rotating each of
three master curves about its junction gives its mate, so a foot on one piece
sits in a recess on the next. The rectangular repeat contains two sets of three
animals. Units are the distance between consecutive rotation centres.
"""
from dataclasses import dataclass
from math import ceil, sqrt

import numpy as np


SQRT3 = sqrt(3)
EYE_RADIUS = 0.017


@dataclass
class LizardTile:
    outline: np.ndarray           # unclosed, all edge samples in boundary order
    edges: tuple[np.ndarray, ...] # each includes both junctions
    spine: np.ndarray             # one open engraved stroke
    eyes: np.ndarray              # two centres


def _beziers(start, segments):
    """Sample cubic segments, including each internal endpoint exactly once."""
    curve = [np.asarray(start, dtype=float)[None, :]]
    for control1, control2, end in segments:
        controls = np.array([curve[-1][-1], control1, control2, end])
        length_bound = np.linalg.norm(np.diff(controls, axis=0), axis=1).sum()
        steps = max(4, ceil(length_bound / 0.030))
        t = (np.arange(1, steps + 1) / steps)[:, None]
        s = 1 - t
        curve.append(s**3 * controls[0] + 3*s**2*t * controls[1]
                     + 3*s*t**2 * controls[2] + t**3 * controls[3])
    return np.vstack(curve)


def _turn(points, centre, direction):
    """Rotate one third-turn (+1 or -1) or keep the original orientation."""
    if direction == 0:
        return points.copy()
    cosine, sine = -0.5, direction * SQRT3 / 2
    return (points - centre) @ np.array([[cosine, sine], [-sine, cosine]]) + centre


def _template():
    cheek = np.array([0.0, 0.0])
    forefoot = np.array([1.0, 0.0])
    hindfoot = np.array([0.5, SQRT3 / 2])
    wrist = np.array([0.50, -0.36])
    hip = _turn(wrist, forefoot, -1)

    # The crown, neck and inside foreleg form one continuous shared cut.
    head = _beziers(cheek, [
        ((-.010, -.095), (.022, -.282), (.073, -.345)),
        ((.092, -.373), (.118, -.377), (.160, -.360)),
        ((.222, -.338), (.314, -.282), (.337, -.235)),
        ((.360, -.198), (.318, -.157), (.336, -.107)),
        ((.350, -.054), (.402, .014), (.464, .041)),
        ((.504, .059), (.532, .047), (.546, .010)),
        ((.569, -.043), (.525, -.059), (.502, -.073)),
        ((.483, -.092), (.503, -.257), wrist),
    ])
    # The pad bends back towards the elbow to make the mechanical interlock.
    leg = _beziers(wrist, [
        ((.628, -.376), (.622, -.251), (.684, -.190)),
        ((.722, -.151), (.777, -.143), (.785, -.074)),
        ((.807, -.005), (.765, .007), (.716, .028)),
        ((.661, .047), (.639, .113), (.682, .137)),
        ((.786, .181), (.959, .107), forefoot),
    ])
    tail = _beziers(hip, [
        ((.923, .713), (1.004, .847), (.986, .976)),
        ((.978, 1.047), (.908, 1.106), (.825, 1.101)),
        ((.787, 1.099), (.801, 1.077), (.827, 1.029)),
        ((.905, .863), (.803, .763), (.676, .704)),
        ((.574, .653), (.513, .612), (.472, .664)),
        ((.431, .715), (.462, .801), hindfoot),
    ])
    edges = (head, leg, _turn(leg[::-1], forefoot, -1), tail,
             _turn(tail[::-1], hindfoot, -1), _turn(head[::-1], cheek, 1))
    spine = _beziers((.122, -.314), [
        ((.198, -.083), (.332, .106), (.543, .260)),
        ((.745, .405), (.822, .505), (.878, .719)),
        ((.934, .919), (.961, 1.006), (.851, 1.074)),
    ])
    eyes = np.array([(.101, -.135), (.248, -.192)])
    return edges, spine, eyes


def make_tiles(cols=9, rows=4):
    """Return rectangular period vectors and one whole copy of every tile.

    The centroid used for placing each piece is within the rectangular board.
    Its outline can extend beyond the board: translate a neighbour by either
    period (or both) to recover the same shared curve on the opposite side.
    """
    if not isinstance(cols, int) or not isinstance(rows, int) or min(rows, cols) < 1:
        raise ValueError("cols and rows must be positive integers")
    master_edges, master_spine, master_eyes = _template()
    repeat = np.array([SQRT3, 3.0])
    periods = np.diag(repeat * [cols, rows])
    tiles = []
    for row in range(rows):
        for col in range(cols):
            for centre in (np.array([0., 0.]), np.array([1.5, SQRT3 / 2])):
                for orientation in (0, 1, -1):
                    def place(points):
                        q = _turn(points, np.zeros(2), orientation) + centre
                        return np.column_stack((-q[:, 1], q[:, 0]))

                    edges = tuple(place(edge) for edge in master_edges)
                    outline = np.concatenate([edge[:-1] for edge in edges])
                    # Use the same lift for the whole piece, including its artwork.
                    shift = repeat * ([col, row] - np.floor(outline.mean(0) / repeat))
                    tiles.append(LizardTile(outline + shift, tuple(e + shift for e in edges),
                                            place(master_spine) + shift, place(master_eyes) + shift))
    return periods, tiles
