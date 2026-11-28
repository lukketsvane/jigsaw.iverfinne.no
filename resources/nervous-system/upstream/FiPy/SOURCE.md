# Kobayashi phase-field comparison

NIST's FiPy example evolves one circular crystal seed in an undercooled liquid. Heat diffuses away as the crystal grows, and the orientation of the interface affects its growth rate. The example compares the phase fraction at every grid cell with reference output from Ryo Kobayashi's Fortran code. The 40 × 40 grid and ten time steps are sized for numerical comparison. A visible dendrite takes a larger grid and a longer run. [FiPy example documentation](https://pages.nist.gov/fipy/en/4.0.3/generated/examples.phase.anisotropyOLD.html)

Nervous System adapted this family of equations to give each puzzle piece a phase and a temperature of its own. This allows two neighbors to grow into each other along the same boundary. FiPy supplies a reference for the underlying solidification model. The studio's paper describes the multiphase extension and piece initialization. Its companion project page explains how the studio removes narrow features and smooths the cut paths. [Puzzle paper](https://n-e-r-v-o-u-s.com/education/papers/nervousSystem_2012-jigsaw-puzzle-paper.pdf) · [Project page](https://n-e-r-v-o-u-s.com/works/puzzles/)

## Source files

The files below come from NIST FiPy 4.0.3, revision `64635bf8ed6bc51431edb7d6cf1495ff68ac18a7`. The Git blob identifiers include Git's object header and were checked against the upstream tree. The license permits copying and modification with the notice and warranty disclaimer. [Release](https://github.com/usnistgov/fipy/releases/tag/4.0.3)

| File | Bytes | Git blob SHA-1 |
| --- | ---: | --- |
| [anisotropyOLD.py](https://raw.githubusercontent.com/usnistgov/fipy/64635bf8ed6bc51431edb7d6cf1495ff68ac18a7/examples/phase/anisotropyOLD.py) | 6,984 | `8f72e4d7bb3aa444010d08e0db14370b54c36a23` |
| [anisotropyOLD.gz](https://raw.githubusercontent.com/usnistgov/fipy/64635bf8ed6bc51431edb7d6cf1495ff68ac18a7/examples/phase/anisotropyOLD.gz) | 13,056 | `bef267e03bd9f4c68d1bbac459327d53417c58cf` |
| [LICENSE.rst](https://raw.githubusercontent.com/usnistgov/fipy/64635bf8ed6bc51431edb7d6cf1495ff68ac18a7/LICENSE.rst) | 1,878 | `6dc2bac497ba76402ff0140388a7e5e789a0ab62` |
| [DISCLAIMER.rst](https://raw.githubusercontent.com/usnistgov/fipy/64635bf8ed6bc51431edb7d6cf1495ff68ac18a7/DISCLAIMER.rst) | 487 | `be6c2c90eacf992edc4c9780bd79571efc1e9e27` |

## Reproduce the numerical check

From this directory, run the following in a Python virtual environment.

```sh
python -m pip install fipy==4.0.3
python -B -m doctest -v anisotropyOLD.py
```

All 49 doctest examples passed in an isolated Windows environment with Python 3.11.9, FiPy 4.0.3, NumPy 2.4.6 and SciPy 1.17.1. The headless run used the SciPy solver. After ten time steps, FiPy's `allclose` check passed against all 1,600 reference phase values in `anisotropyOLD.gz`.
