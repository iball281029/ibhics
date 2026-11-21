/* SVG storage ring. All glyphs stay upright: only their anchor positions rotate. */
(() => {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let instance;
  function create(host) {
    let angle = 0, frame = 0, previous = 0, pointer = null, hovered = false;
    let startX = 0, startY = 0, lastAngle = 0, moved = false, suppressClick = false;
    const page = document.getElementById('page-storage');
    function active() {
      return host.isConnected && !document.hidden && page?.classList.contains('active') && !host.closest('[hidden]');
    }
    function position() {
      host.querySelectorAll('[data-ring-index]').forEach((node) => {
        const radians = (angle - 90 + Number(node.dataset.ringIndex) * 60) * Math.PI / 180;
        node.setAttribute('transform', `translate(${420 + Math.cos(radians) * 244} ${364 + Math.sin(radians) * 244})`);
      });
    }
    function tick(now) {
      frame = 0;
      if (!active() || reduced.matches || hovered || pointer !== null) return;
      angle = (angle + (previous ? Math.min(50, now - previous) * .002 : 0)) % 360;
      previous = now;
      position();
      frame = requestAnimationFrame(tick);
    }
    function sync() {
      cancelAnimationFrame(frame); frame = 0; previous = 0;
      const animate = active() && !reduced.matches && !hovered && pointer === null;
      host.classList.toggle('vs-motion-paused', !animate);
      if (animate) frame = requestAnimationFrame(tick);
    }
    function polar(event) {
      const rect = host.querySelector('svg').getBoundingClientRect();
      return Math.atan2(event.clientY - rect.top - rect.height * 364 / 760,
        event.clientX - rect.left - rect.width / 2) * 180 / Math.PI;
    }
    function down(event) {
      if (!active() || reduced.matches || event.isPrimary === false || event.button !== 0 || pointer !== null) return;
      if (event.target.closest('.vs-ring-core')) return;
      pointer = event.pointerId; startX = event.clientX; startY = event.clientY;
      lastAngle = polar(event); moved = false; suppressClick = false; sync();
    }
    function move(event) {
      if (event.pointerId !== pointer) return;
      if (!moved && Math.hypot(event.clientX - startX, event.clientY - startY) < 5) return;
      moved = true;
      host.setPointerCapture(pointer);
      const next = polar(event);
      angle += ((next - lastAngle + 540) % 360) - 180;
      lastAngle = next; position();
    }
    function up(event) {
      if (event.pointerId !== pointer) return;
      suppressClick = moved;
      if (host.hasPointerCapture(pointer)) host.releasePointerCapture(pointer);
      pointer = null; sync();
    }
    function cancel(event) { up(event); suppressClick = false; }
    function click(event) {
      if (suppressClick && event.detail !== 0) { event.preventDefault(); event.stopPropagation(); }
      suppressClick = false;
    }
    function key(event) {
      if (!event.target.closest('.vs-ring-rotation') || !['ArrowLeft', 'ArrowRight'].includes(event.key) || reduced.matches) return;
      event.preventDefault(); angle += event.key === 'ArrowRight' ? 15 : -15; position();
    }
    const enter = () => { hovered = true; sync(); };
    const leave = () => {
      hovered = false;
      if (pointer !== null && !host.hasPointerCapture(pointer)) pointer = null;
      sync();
    };
    const events = {pointerdown: down, pointermove: move, pointerup: up, pointercancel: cancel,
      lostpointercapture: up, pointerenter: enter, pointerleave: leave, click, keydown: key};
    Object.entries(events).forEach(([name, handler]) => host.addEventListener(name, handler));
    const observer = new MutationObserver(sync);
    observer.observe(page, {attributes: true, subtree: true, attributeFilter: ['class', 'hidden']});
    document.addEventListener('visibilitychange', sync);
    reduced.addEventListener('change', sync);
    return {host, update() { position(); sync(); }, dispose() {
      cancelAnimationFrame(frame); observer.disconnect();
      Object.entries(events).forEach(([name, handler]) => host.removeEventListener(name, handler));
      document.removeEventListener('visibilitychange', sync); reduced.removeEventListener('change', sync);
    }};
  }
  window.StorageScene = {mount(host) {
    if (instance?.host !== host) { instance?.dispose(); instance = host ? create(host) : null; }
    instance?.update();
  }};
  window.VirtualStorageUI?.mountScene();
})();
