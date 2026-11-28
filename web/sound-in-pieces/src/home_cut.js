// The inline board uses the page's tap melody. Generating and reshaping the cut are silent.
function tick() {}
function crackle() {}

if (!svg || !svg.hasAttribute('data-inline')) return;
count = 64;
const local = (x, y) => {
  const r = svg.getBoundingClientRect();
  return [(x - r.left) * W / r.width, (y - r.top) * H / r.height];
};

function render(work) {
  const version = gen;
  svg.dataset.state = 'building';
  work.then(() => {
    if (version === gen) svg.dataset.state = 'ready';
  }).catch(error => {
    if (version !== gen) return;
    clear(0);
    current = board = null;
    svg.classList.remove('dev');
    svg.dataset.state = 'error';
    console.error('Could not generate this puzzle cut', error);
  });
}
function reseed(at) {
  visible.disconnect();
  render(build((Math.random() * 2 ** 31) >>> 0, at));
}
const visible = new IntersectionObserver(entries => {
  if (svg.dataset.state === 'idle' && entries.some(entry => entry.isIntersecting)) reseed();
});

let gesture = null, ignoreClick = false;
function cancelGesture() {
  if (!gesture) return;
  cancelAnimationFrame(gesture.frame);
  if (gesture.previousCut) {
    for (const piece of drag.moved) delete piece.z2;
    drag = null;
    board.ghost.remove();
    gesture.previousCut.classList.add('set');
    svg.append(gesture.previousCut);
    svg.classList.remove('dev');
    svg.dataset.state = 'ready';
  }
  gesture = null;
}
svg.addEventListener('pointerdown', e => {
  if (!e.isPrimary || e.button !== 0) {
    ignoreClick = true;
    cancelGesture();
    return;
  }
  ignoreClick = false;
  gesture = { id: e.pointerId, x: e.clientX, y: e.clientY, frame: 0 };
  svg.setPointerCapture(e.pointerId);
});
svg.addEventListener('pointermove', e => {
  if (gesture?.id !== e.pointerId) return;
  if (!gesture.previousCut) {
    const dx = e.clientX - gesture.x, dy = e.clientY - gesture.y;
    if (Math.hypot(dx, dy) < 7) return;
    ignoreClick = true;
    if ((e.pointerType === 'touch' && Math.abs(dy) >= Math.abs(dx)) || svg.dataset.state !== 'ready') {
      cancelGesture();
      return;
    }
    // Keep the last cut so a cancelled drag can restore it without repartitioning.
    const previousCut = svg.querySelector('g.cut:not(.gone)');
    if (!previousCut) { cancelGesture(); return; }
    previousCut.remove();
    grab(local(gesture.x, gesture.y));
    gesture.previousCut = previousCut;
    svg.dataset.state = 'dragging';
  }
  gesture.at = local(e.clientX, e.clientY);
  if (!gesture.frame) gesture.frame = requestAnimationFrame(() => {
    gesture.frame = 0;
    knead(gesture.at);
  });
});
svg.addEventListener('pointerup', e => {
  if (gesture?.id !== e.pointerId) return;
  if (gesture.previousCut) {
    cancelAnimationFrame(gesture.frame);
    knead(local(e.clientX, e.clientY));
    render(release([e.clientX, e.clientY]));
  }
  gesture = null;
});
function cancelPointer(e) {
  if (gesture?.id !== e.pointerId) return;
  if (e.type === 'lostpointercapture' && e.target !== svg) return;
  ignoreClick = true;
  cancelGesture();
}
svg.addEventListener('pointercancel', cancelPointer);
svg.addEventListener('lostpointercapture', cancelPointer);

function activate(at) {
  cancelGesture();
  window.playNextChord();
  reseed(at);
}
svg.addEventListener('click', e => {
  if (e.detail && ignoreClick) return;
  activate(e.detail ? [e.clientX, e.clientY] : undefined);
});
svg.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  e.preventDefault();
  if (!e.repeat) activate();
});
new ResizeObserver(widths).observe(svg);
svg.setAttribute('role', 'button');
svg.setAttribute('tabindex', '0');
svg.setAttribute('aria-label', 'Generate a new cut and play the next chord');
svg.setAttribute('aria-description', 'Drag sideways to reshape the cut.');
svg.setAttribute('data-interactive', '');
svg.dataset.state = 'idle';
visible.observe(svg);
})();
