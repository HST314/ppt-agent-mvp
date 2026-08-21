(() => {
  const slides = [...document.querySelectorAll('.slide')];
  const previous = document.getElementById('offline-prev');
  const next = document.getElementById('offline-next');
  const page = document.getElementById('offline-page');
  const root = document.documentElement;
  const CONTROLS_CLEARANCE = 76;
  const metrics = {navigationCount: 0, slideStateMutations: 0, fitCount: 0, resizeRequests: 0};
  const dimensions = new WeakMap();
  let index = 0;
  let activeSlide = null;
  let resizeFrame = 0;
  window.__offlinePlayerMetrics = metrics;
  document.body.classList.add('offline-player');
  function fit() {
    const slide = slides[index];
    if (!slide) return;
    let size = dimensions.get(slide);
    if (!size) {
      size = {width: slide.offsetWidth || 1280, height: slide.offsetHeight || 720};
      dimensions.set(slide, size);
    }
    const {width, height} = size;
    const available = Math.max(0, window.innerHeight - CONTROLS_CLEARANCE);
    let scale = Math.min(window.innerWidth / width, available / height);
    if (!Number.isFinite(scale) || scale <= 0) scale = Math.min(window.innerWidth / width, window.innerHeight / height);
    root.style.setProperty('--offline-scale', String(scale));
    root.style.setProperty('--offline-center-y', `${available / 2}px`);
    metrics.fitCount += 1;
  }
  function show(target) {
    const nextIndex = Math.max(0, Math.min(slides.length - 1, target));
    const nextSlide = slides[nextIndex] || null;
    if (activeSlide && activeSlide !== nextSlide) {
      activeSlide.setAttribute('aria-hidden', 'true');
      activeSlide.removeAttribute('aria-current');
      metrics.slideStateMutations += 1;
    }
    index = nextIndex;
    activeSlide = nextSlide;
    if (activeSlide) {
      activeSlide.setAttribute('aria-hidden', 'false');
      activeSlide.setAttribute('aria-current', 'page');
      metrics.slideStateMutations += 1;
    }
    page.value = slides.length ? `${index + 1} / ${slides.length}` : '0 / 0';
    previous.disabled = index === 0;
    next.disabled = !slides.length || index === slides.length - 1;
    history.replaceState(null, '', `#slide=${index + 1}`);
    fit();
    metrics.navigationCount += 1;
    window.dispatchEvent(new CustomEvent('offline-slide-change', {detail: {index, total: slides.length}}));
  }
  previous.addEventListener('click', () => show(index - 1));
  next.addEventListener('click', () => show(index + 1));
  addEventListener('keydown', event => {
    if (event.target.matches('input,textarea,select,[contenteditable="true"]')) return;
    if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); show(index + 1); }
    else if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key)) { event.preventDefault(); show(index - 1); }
    else if (event.key === 'Home') { event.preventDefault(); show(0); }
    else if (event.key === 'End') { event.preventDefault(); show(slides.length - 1); }
  });
  addEventListener('resize', () => {
    metrics.resizeRequests += 1;
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => { resizeFrame = 0; fit(); });
  }, {passive: true});
  const requested = Number(new URLSearchParams(location.hash.slice(1)).get('slide')) - 1;
  show(Number.isInteger(requested) ? requested : 0);
})();
