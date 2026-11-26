"""Locations used by the package. Everything lives under the package directory so the project is self-contained."""
from pathlib import Path

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
CONFIGS = PKG / 'configs'
EXAMPLES = PKG / 'examples'
CACHE = PKG / 'data_cache'            # git-ignored: downloaded originals and derived arrays
RUNS = PKG / 'runs'                   # git-ignored: experiment run directories
CHECKPOINTS = PKG / 'checkpoints'     # small trained models that are committed
PHYLOPIC = CACHE / 'phylopic'
ORIGINALS = PHYLOPIC / 'originals'    # downloaded files only; nothing else is ever written here
BANK = CACHE / 'bank'                 # derived arrays built by data.prepare
