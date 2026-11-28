"""Worked example of the body-plan API (not part of the corpus): a marker-drawn trilobite in two styles.

Read this before writing a plan module. Conventions: local units about one unit across, y down, head at the top
(negative y), the vertical centre line is x = 0. Sample a genome with the rng, draw strokes and blobs into a Canvas,
record what was sampled in canvas.genome, return the canvas. Canvas.finish() does the rest (and may raise Reject).

Gaps between parts must stay open: two strokes of radius r whose centre lines are d apart leave a gap d - 2r, and the
smooth union (radius k) plus the wobble close gaps narrower than about k + 2 * wobble. A gap should be at least as
wide as the parts beside it, or the piece gets bigger (size factor) to keep its notches cuttable.
"""
import numpy as np

from morph.core import Canvas, arc, bend, catmull, compose, mirror, taper, wiggle


def rib_skeleton(rng):
    """dome head whose rim runs down as genal spines, a spine, a few paired ribs: the stroke style of drawing 42eb5982"""
    ribs = int(rng.integers(3, 6)); r = rng.uniform(.034, .045); hw = rng.uniform(.36, .46)
    c = Canvas(k=.015, wobble=.003, warp=compose(bend(rng.uniform(-.5, .5)), taper(rng.uniform(-.25, .25))))
    rim = catmull([(-hw, .38), (-hw * 1.02, .05), (-hw * .8, -.25), (-.25, -.4), (0, -.45), (.25, -.4), (hw * .8, -.25), (hw * 1.02, .05), (hw, .38)], 70)
    c.stroke(wiggle(rng, rim), r * 1.15)
    c.blob(np.vstack([arc((0, -.08), hw * .82, np.pi, 2 * np.pi, 40), [(hw * .7, -.06), (-hw * .7, -.06)]]))   # filled head shield
    spine = np.stack([rng.normal(0, .008, 10), np.linspace(-.1, .5, 10)], 1)
    c.stroke(wiggle(rng, spine), r)
    top, bot = .03, .42
    for i in range(ribs):
        y = top + (bot - top) * i / max(ribs - 1, 1); L = (hw - 2.4 * r) * (1 - .45 * i / ribs)
        rib = catmull([(0, y), (L * .55, y + .015), (L, y + .045)], 14)
        c.stroke(wiggle(rng, rib, .004), r); c.stroke(mirror(wiggle(rng, rib, .004)), r)
    c.feature_stroke([(-.17, -.24), (-.08, -.245)], .014); c.feature_stroke([(.08, -.245), (.17, -.24)], .014)   # sleepy eyes
    c.feature_stroke(catmull([(-.035, -.13), (0, -.155), (.035, -.13)], 8), .011)                              # mouth
    c.genome = dict(ribs=ribs, radius=r, half_width=hw)
    return c


def lobed(rng):
    """filled body with rounded pleural lobes, like the crescent and fan-tail drawings"""
    n = int(rng.integers(3, 6)); w = rng.uniform(.26, .34); seg = .52 / n
    c = Canvas(k=.02, wobble=.004, warp=compose(bend(rng.uniform(-.7, .7)), taper(rng.uniform(-.2, .2))))
    c.blob(np.vstack([arc((0, -.12), w + .08, np.pi, 2 * np.pi, 40), [(w + .12, -.1), (-w - .12, -.1)]]))       # head shield
    for s in (1, -1):
        c.stroke(catmull([(s * (w + .06), -.14), (s * (w + .12), .02), (s * (w + .07), .16)], 16), np.linspace(.055, .035, 16))   # genal spines
    c.stroke([(0, -.12), (0, .5)], .075)                                                                         # axis
    for i in range(n):
        y = .0 + seg * (i + .5); half = w * (1 - .55 * i / n) * rng.uniform(.9, 1.1)
        for s in (1, -1): c.stroke(catmull([(0, y), (s * half * .6, y + .01), (s * half, y + .05)], 10), seg * .3)   # lobes with gaps between
    c.eye((-.13, -.22), .024); c.eye((.13, -.22), .024)
    c.genome = dict(segments=n, width=w)
    return c


PLANS = {'rib-skeleton': (1., rib_skeleton), 'lobed': (1., lobed)}
