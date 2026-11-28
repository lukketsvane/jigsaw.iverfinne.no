# PositionBasedDynamics rod constraints

Nervous System credits PositionBasedDynamics and describes how its Maze puzzle edges grow, bend and collide inside the puzzle outline. This reference snapshot contains upstream C++ source for the rod physics. The studio does not identify its exact revision or publish its puzzle-specific growth and layout code in the article. [Studio article, October 24, 2017](https://n-e-r-v-o-u-s.com/blog/?p=7821)

The upstream revision selected for reference is `39a5099c39df84494df14aaad6fb9af845793ff7`, the last revision on the upstream default branch before the article appeared. It dates to July 5, 2017 and carries the MIT license. [Upstream commit](https://github.com/InteractiveComputerGraphics/PositionBasedDynamics/commit/39a5099c39df84494df14aaad6fb9af845793ff7)

Start with `PositionBasedCosseratRods::solve_StretchShearConstraint` and `solve_BendTwistConstraint` in the implementation. The demo shows how a rod model connects those constraints along a curve. The [complete upstream project ZIP](PositionBasedDynamics-39a5099c39df.zip) includes the build files, demos, model data and bundled libraries with their individual notices. Its bundled Eigen version header identifies 3.3.4.

All 623 files in the ZIP matched the upstream Git blobs. The archive is 4,798,169 bytes with SHA-256 `985ffac98adc18e880deccfba5c5999a616ed70e4d978f3d9032bc645d7067bb`. [Original archive](https://codeload.github.com/InteractiveComputerGraphics/PositionBasedDynamics/zip/39a5099c39df84494df14aaad6fb9af845793ff7)

Each local file was compared to the corresponding Git blob in the pinned tree. The filenames link to the exact upstream raw bytes.

| File | Bytes | Git blob SHA-1 |
| --- | ---: | --- |
| [LICENSE](https://raw.githubusercontent.com/InteractiveComputerGraphics/PositionBasedDynamics/39a5099c39df84494df14aaad6fb9af845793ff7/LICENSE) | 1111 | `f14280d6e0ebf61ca22541ea915e7aff84e15e9f` |
| [PositionBasedDynamics/PositionBasedElasticRods.h](https://raw.githubusercontent.com/InteractiveComputerGraphics/PositionBasedDynamics/39a5099c39df84494df14aaad6fb9af845793ff7/PositionBasedDynamics/PositionBasedElasticRods.h) | 8062 | `f39a89b71bf52f563911b6f08eb5a8bedc6f42ea` |
| [PositionBasedDynamics/PositionBasedElasticRods.cpp](https://raw.githubusercontent.com/InteractiveComputerGraphics/PositionBasedDynamics/39a5099c39df84494df14aaad6fb9af845793ff7/PositionBasedDynamics/PositionBasedElasticRods.cpp) | 13745 | `232cff6466b756fb60adb1e9559a35ef2e9aa7d5` |
| [Demos/CosseratRodsDemo/main.cpp](https://raw.githubusercontent.com/InteractiveComputerGraphics/PositionBasedDynamics/39a5099c39df84494df14aaad6fb9af845793ff7/Demos/CosseratRodsDemo/main.cpp) | 14909 | `b572d2693c48b95176b14a5a5aee7bb9d8140d20` |
