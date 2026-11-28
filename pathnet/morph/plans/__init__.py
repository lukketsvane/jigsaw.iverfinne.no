"""Body plans for the FossilNet v3 corpus. Each module defines PLANS = {name: (weight, draw)} where draw(rng) returns a
morph.core.Canvas with the creature drawn into it (the canvas genome dict says what was sampled). GROUPS weighs the
modules against each other in the corpus; a module that fails to import is skipped with a warning."""
import importlib, sys

GROUPS = {               # module: share of the corpus
    'trilobite': .26,
    'arthropod': .14,
    'shell': .11,
    'echinoderm': .11,
    'coral': .12,
    'dendrite': .09,
    'frond': .08,
    'oddity': .09,
}


def load(names=None):
    """{plan name: (probability, draw)} over the modules that import, probabilities summing to 1"""
    out = {}
    for mod in (names or GROUPS):
        try: m = importlib.import_module(f'morph.plans.{mod}')
        except Exception as e:  # noqa: BLE001 (one broken module must not stop the corpus)
            print(f'morph.plans.{mod}: {type(e).__name__}: {e}', file=sys.stderr); continue
        plans = getattr(m, 'PLANS', {}); tot = sum(w for w, _ in plans.values()) or 1
        for name, (w, fn) in plans.items(): out[f'{mod}.{name}'] = (GROUPS.get(mod, .1) * w / tot, fn)
    s = sum(p for p, _ in out.values()) or 1
    return {k: (p / s, fn) for k, (p, fn) in out.items()}
