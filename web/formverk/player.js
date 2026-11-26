/* Formverk Interactive Jigsaw Play Tester — drag, magnetic neighbor snapping, group movement. */
(function (global) {
  'use strict';
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function el(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
    return node;
  }

  function polyD(pts) {
    if (!pts || !pts.length) return '';
    return 'M' + pts.map(p => `${p[0]},${p[1]}`).join('L') + 'Z';
  }

  function lineD(pts) {
    if (!pts || pts.length < 2) return '';
    return 'M' + pts.map(p => `${p[0]},${p[1]}`).join('L');
  }

  class PuzzlePlayer {
    constructor(svg, layer, onStatus) {
      this.svg = svg;
      this.layer = layer;
      this.onStatus = onStatus || (() => {});
      this.puzzle = null;
      this.offsets = [];
      this.groups = [];
      this.nodes = [];
      this.drag = null;
      this.snapTol = 6.0;
      this._boundMove = e => this._onMove(e);
      this._boundUp = () => this._onUp();
    }

    load(puzzle) {
      this.puzzle = puzzle;
      const n = puzzle.pieces.length;
      this.offsets = Array.from({ length: n }, () => [0, 0]);
      this.groups = Array.from({ length: n }, (_, i) => i);
      this.snapTol = Math.max(4.5, Math.min(11.0, Math.hypot(puzzle.width_mm, puzzle.height_mm) * 0.025));
      this.render();
      this._emitStatus();
    }

    scatter(mode) {
      if (!this.puzzle) return;
      const n = this.puzzle.pieces.length;
      const W = this.puzzle.width_mm;
      const H = this.puzzle.height_mm;
      this.offsets = Array.from({ length: n }, () => [0, 0]);
      this.groups = Array.from({ length: n }, (_, i) => (mode === 'few' ? 0 : i));
      const indices = Array.from({ length: n }, (_, i) => i);
      for (let i = n - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [indices[i], indices[j]] = [indices[j], indices[i]];
      }
      const loose = mode === 'few' ? indices.slice(0, Math.min(8, n)) : indices;
      loose.forEach((pid, k) => {
        this.groups[pid] = pid + 1000;
        const side = k % 2 === 0 ? -1 : 1;
        const dx = side * (W * 0.42 + (k % 5) * 14);
        const dy = ((k / Math.max(1, loose.length)) - 0.5) * H * 0.85;
        this.offsets[pid] = [dx, dy];
      });
      this._updateTransforms();
      this._emitStatus();
    }

    assemble() {
      if (!this.puzzle) return;
      const n = this.puzzle.pieces.length;
      this.offsets = Array.from({ length: n }, () => [0, 0]);
      this.groups = Array.from({ length: n }, () => 0);
      this._updateTransforms();
      this._emitStatus();
    }

    render() {
      this.layer.innerHTML = '';
      this.nodes = [];
      if (!this.puzzle) return;

      // Subtle guide outline of assembled board
      const W = this.puzzle.width_mm;
      const H = this.puzzle.height_mm;
      this.layer.appendChild(
        el('rect', {
          x: 0, y: 0, width: W, height: H,
          fill: '#00000006', stroke: '#b8b2a2', 'stroke-width': 0.5, 'stroke-dasharray': '3 3',
        })
      );

      this.puzzle.pieces.forEach((p, idx) => {
        const g = el('g', { class: 'play-piece', 'data-id': idx });
        g.appendChild(
          el('path', {
            d: polyD(p.outline),
            fill: p.color,
            stroke: '#251e18',
            'stroke-width': 0.35,
            'stroke-linejoin': 'round',
          })
        );
        (p.engrave || []).forEach(pts => {
          g.appendChild(
            el('path', {
              d: lineD(pts),
              fill: 'none',
              stroke: '#5e3c1e',
              'stroke-width': 0.35,
              'stroke-linecap': 'round',
            })
          );
        });
        g.addEventListener('pointerdown', e => this._onDown(e, idx));
        this.layer.appendChild(g);
        this.nodes.push(g);
      });
      this._updateTransforms();
    }

    _svgPoint(e) {
      const pt = this.svg.createSVGPoint();
      pt.x = e.clientX;
      pt.y = e.clientY;
      return pt.matrixTransform(this.layer.getScreenCTM().inverse());
    }

    _onDown(e, idx) {
      if (!this.puzzle) return;
      e.stopPropagation();
      const sp = this._svgPoint(e);
      const gid = this.groups[idx];
      const members = [];
      this.groups.forEach((g, i) => {
        if (g === gid) {
          members.push(i);
          this.layer.appendChild(this.nodes[i]);
          this.nodes[i].classList.add('dragging');
        }
      });
      this.drag = {
        members,
        start: [sp.x, sp.y],
        orig: members.map(i => [...this.offsets[i]]),
      };
      window.addEventListener('pointermove', this._boundMove);
      window.addEventListener('pointerup', this._boundUp);
    }

    _onMove(e) {
      if (!this.drag) return;
      const sp = this._svgPoint(e);
      const dx = sp.x - this.drag.start[0];
      const dy = sp.y - this.drag.start[1];
      this.drag.members.forEach((pid, k) => {
        this.offsets[pid] = [this.drag.orig[k][0] + dx, this.drag.orig[k][1] + dy];
      });
      this._updateTransforms();
    }

    _onUp() {
      if (!this.drag) return;
      this.drag.members.forEach(pid => this.nodes[pid].classList.remove('dragging'));
      this._trySnap(this.drag.members);
      this.drag = null;
      window.removeEventListener('pointermove', this._boundMove);
      window.removeEventListener('pointerup', this._boundUp);
      this._updateTransforms();
      this._emitStatus();
    }

    _trySnap(moved) {
      const movedSet = new Set(moved);
      const nbrs = this.puzzle.neighbors || {};
      let snapped = false;
      for (const pid of moved) {
        for (const nid of (nbrs[String(pid)] || [])) {
          if (movedSet.has(nid)) continue;
          const dx = this.offsets[nid][0] - this.offsets[pid][0];
          const dy = this.offsets[nid][1] - this.offsets[pid][1];
          if (Math.hypot(dx, dy) <= this.snapTol) {
            // Merge groups and align exact offset!
            const targetG = this.groups[nid];
            const oldG = this.groups[pid];
            for (let i = 0; i < this.groups.length; i++) {
              if (this.groups[i] === oldG) {
                this.offsets[i][0] += dx;
                this.offsets[i][1] += dy;
                this.groups[i] = targetG;
              }
            }
            snapped = true;
            break;
          }
        }
        if (snapped) break;
      }
      // Also snap gently to home board origin if very close
      const first = moved[0];
      if (!snapped && Math.hypot(this.offsets[first][0], this.offsets[first][1]) <= this.snapTol * 0.7) {
        const dx = -this.offsets[first][0];
        const dy = -this.offsets[first][1];
        moved.forEach(pid => {
          this.offsets[pid][0] += dx;
          this.offsets[pid][1] += dy;
        });
      }
    }

    _updateTransforms() {
      this.nodes.forEach((node, idx) => {
        const [ox, oy] = this.offsets[idx];
        node.setAttribute('transform', `translate(${ox.toFixed(2)},${oy.toFixed(2)})`);
      });
    }

    _emitStatus() {
      if (!this.puzzle) return;
      const counts = new Map();
      this.groups.forEach(g => counts.set(g, (counts.get(g) || 0) + 1));
      const largest = Math.max(0, ...counts.values());
      const n = this.puzzle.pieces.length;
      const totalGroups = counts.size;
      this.onStatus({ largest, total: n, groups: totalGroups });
    }
  }

  global.FormverkPlayer = PuzzlePlayer;
})(window);
