(() => {
  const slides = [...document.querySelectorAll('.slide')];
  const previous = document.getElementById('offline-prev');
  const next = document.getElementById('offline-next');
  const page = document.getElementById('offline-page');
  const root = document.documentElement;
  const CONTROLS_CLEARANCE = 76;
  let index = 0;
  document.body.classList.add('offline-player');
  function fit() {
    const slide = slides[index];
    if (!slide) return;
    const width = slide.offsetWidth || 1280;
    const height = slide.offsetHeight || 720;
    const available = Math.max(0, window.innerHeight - CONTROLS_CLEARANCE);
    let scale = Math.min(window.innerWidth / width, available / height);
    if (!Number.isFinite(scale) || scale <= 0) scale = Math.min(window.innerWidth / width, window.innerHeight / height);
    root.style.setProperty('--offline-scale', String(scale));
    root.style.setProperty('--offline-center-y', `${available / 2}px`);
  }
  function show(target) {
    index = Math.max(0, Math.min(slides.length - 1, target));
    slides.forEach((slide, current) => {
      const active = current === index;
      slide.setAttribute('aria-hidden', String(!active));
      if (active) slide.setAttribute('aria-current', 'page'); else slide.removeAttribute('aria-current');
    });
    page.value = slides.length ? `${index + 1} / ${slides.length}` : '0 / 0';
    previous.disabled = index === 0;
    next.disabled = !slides.length || index === slides.length - 1;
    history.replaceState(null, '', `#slide=${index + 1}`);
    fit();
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
  addEventListener('resize', fit);
  const requested = Number(new URLSearchParams(location.hash.slice(1)).get('slide')) - 1;
  show(Number.isInteger(requested) ? requested : 0);
})();
