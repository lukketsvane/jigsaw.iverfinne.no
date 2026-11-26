# PositionBasedDynamics rod constraints

Nervous System credits PositionBasedDynamics and describes how its Maze puzzle edges grow, bend and collide inside the puzzle outline. This reference snapshot contains upstream C++ source for the rod physics. The studio does not identify its exact revision or publish its puzzle-specific growth and layout code in the article. [Studio article, October 24, 2017](https://n-e-r-v-o-u-s.com/blog/?p=7821)

The upstream revision selected for reference is `39a5099c39df84494df14aaad6fb9af845793ff7`, the last revision on the upstream default branch before the article appeared. It dates to July 5, 2017 and carries the MIT license. [Upstream commit](https://github.com/InteractiveComputerGraphics/PositionBasedDynamics/commit/39a5099c39df84494df14aaad6fb9af845793ff7)

Start with `PositionBasedCosseratRods::solve_StretchShearConstraint` and `solve_BendTwistConstraint` in the implementation. The demo shows how a rod model connects those constraints along a curve. Building the demo requires the full upstream project.

Each local file was compared to the corresponding Git blob in the pinned tree. The filenames link to the exact upstream raw bytes.

| File | Bytes | Git blob SHA-1 |
| --- | ---: | --- |
| [LICENSE](https://raw.githubusercontent.com/InteractiveComputerGraphics/PositionBasedDynamics/39a5099c39df84494df14aaad6fb9af845793ff7/LICENSE) | 1111 | `f14280d6e0ebf61ca22541ea915e7aff84e15e9f` |
| [PositionBasedDynamics/PositionBasedElasticRods.h](https://raw.githubusercontent.com/InteractiveComputerGraphics/PositionBasedDynamics/39a5099c39df84494df14aaad6fb9af845793ff7/PositionBasedDynamics/PositionBasedElasticRods.h) | 8062 | `f39a89b71bf52f563911b6f08eb5a8bedc6f42ea` |
| [PositionBasedDynamics/PositionBasedElasticRods.cpp](https://raw.githubusercontent.com/InteractiveComputerGraphics/PositionBasedDynamics/39a5099c39df84494df14aaad6fb9af845793ff7/PositionBasedDynamics/PositionBasedElasticRods.cpp) | 13745 | `232cff6466b756fb60adb1e9559a35ef2e9aa7d5` |
| [Demos/CosseratRodsDemo/main.cpp](https://raw.githubusercontent.com/InteractiveComputerGraphics/PositionBasedDynamics/39a5099c39df84494df14aaad6fb9af845793ff7/Demos/CosseratRodsDemo/main.cpp) | 14909 | `b572d2693c48b95176b14a5a5aee7bb9d8140d20` |
