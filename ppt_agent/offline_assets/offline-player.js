(() => {
  const slides = [...document.querySelectorAll('.slide')];
  const previous = document.getElementById('offline-prev');
  const next = document.getElementById('offline-next');
  const page = document.getElementById('offline-page');
  const root = document.documentElement;
  const CONTROLS_CLEARANCE = 76;
  const metrics = {navigationCount: 0, slideStateMutations: 0, fitCount: 0, resizeRequests: 0};
  const dimensions = new WeakMap();
  let activeSlide = null;
  let resizeFrame = 0;
  window.__offlinePlayerMetrics = metrics;
  const LOW_POWER_KEY = 'ppt-agent-low-power';
  const motion = window.Motion || null;
  const animate = motion ? motion.animate : null;
  const stagger = motion ? motion.stagger : null;
  let index = 0;
  let overviewOn = false;
  document.body.classList.add('offline-player');

  /* ---------- 低功耗/静态模式: B 切换, localStorage 持久化, 默认跟随系统减弱动态 ---------- */
  const storage = {
    get() { try { return localStorage.getItem(LOW_POWER_KEY); } catch (error) { return null; } },
    set(value) { try { localStorage.setItem(LOW_POWER_KEY, value); } catch (error) { /* file:// 下可能禁用,忽略 */ } }
  };
  const prefersReduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const stored = storage.get();
  let lowPower = stored === '1' || (stored === null && prefersReduced);
  const hint = document.getElementById('offline-hint');

  /* 缓动从 :root 的 motion token 读取,CSS 是唯一事实源(缺失时回退通用缓动) */
  const cssEase = (name, fallback) => {
    const value = getComputedStyle(root).getPropertyValue(name);
    const matched = value && value.match(/cubic-bezier\(([^)]+)\)/);
    const nums = matched ? matched[1].split(',').map(Number) : [];
    return (nums.length === 4 && nums.every(Number.isFinite)) ? nums : fallback;
  };
  const EASE = cssEase('--ease-entry-exp', [.22, 1, .36, 1]);

  function currentSlide() { return slides[index] || null; }

  function cancelSlideAnimations(slide) {
    if (slide && slide.getAnimations) slide.getAnimations({subtree: true}).forEach(item => item.cancel());
  }

  function revealStatic(slide) {
    cancelSlideAnimations(slide);
    slide.querySelectorAll('[data-anim]').forEach(el => {
      el.style.opacity = '1';
      el.style.transform = 'none';
    });
  }

  function resetAnims(slide) {
    cancelSlideAnimations(slide);
    slide.querySelectorAll('[data-anim]').forEach(el => {
      el.style.opacity = '';
      el.style.transform = '';
    });
  }

  /* ---------- pipeline 手动推进: 步骤先逐项亮起, 全部亮完才翻页 ---------- */
  let pipeStep = -1;

  function primePipeline(slide) {
    pipeStep = -1;
    slide.querySelectorAll('[data-anim="step"],[data-anim="arrow"]').forEach(el => {
      el.style.opacity = '0.15';
      el.style.transform = 'none';
    });
  }

  function pipeAdvance() {
    if (lowPower || !motion) return false;
    const slide = currentSlide();
    if (!slide || slide.dataset.animate !== 'pipeline') return false;
    const steps = [...slide.querySelectorAll('[data-anim="step"]')];
    const arrows = [...slide.querySelectorAll('[data-anim="arrow"]')];
    if (!steps.length || pipeStep >= steps.length - 1) return false;
    pipeStep++;
    animate(steps[pipeStep], {opacity: [0.15, 1], y: [8, 0]}, {duration: .5, easing: EASE});
    if (pipeStep > 0 && arrows[pipeStep - 1]) {
      animate(arrows[pipeStep - 1], {opacity: [0.15, .7]}, {duration: .3, delay: .15});
    }
    return true;
  }

  /* ---------- 入场配方: 与模板内置 runtime 同语义 ---------- */
  function playSlide(i) {
    const slide = slides[i];
    if (!slide) return;
    if (!motion || lowPower) { revealStatic(slide); return; }
    const recipe = slide.dataset.animate || (slide.classList.contains('hero') ? 'hero' : 'cascade');
    resetAnims(slide);
    const all = [...slide.querySelectorAll('[data-anim]')];
    if (!all.length) return;

    if (recipe === 'pipeline') {
      const marked = new Set([...slide.querySelectorAll('[data-anim="step"],[data-anim="arrow"]')]);
      if (marked.size) {
        const rest = all.filter(el => !marked.has(el));
        if (rest.length) animate(rest, {opacity: [0, 1], y: [12, 0]}, {duration: .6, delay: stagger(.1, {start: .15}), easing: EASE});
        primePipeline(slide);
        return;
      }
      /* 未标记 step 的 pipeline 页退化为 cascade,避免整页停在暗态 */
    }

    if (recipe === 'directional') {
      const lefts = all.filter(el => el.dataset.anim === 'left');
      const dividers = all.filter(el => el.dataset.anim === 'divider');
      const rights = all.filter(el => el.dataset.anim === 'right');
      const others = all.filter(el => !['left', 'right', 'divider'].includes(el.dataset.anim));
      if (others.length) animate(others, {opacity: [0, 1], y: [12, 0]}, {duration: .6, delay: stagger(.1, {start: .15}), easing: EASE});
      if (lefts.length) animate(lefts, {opacity: [0, 1], x: [-24, 0]}, {duration: .8, delay: .35, easing: EASE});
      if (dividers.length) animate(dividers, {opacity: [0, .25]}, {duration: .5, delay: .9});
      if (rights.length) animate(rights, {opacity: [0, 1], x: [24, 0]}, {duration: .8, delay: 1.0, easing: EASE});
      return;
    }

    if (recipe === 'quote') {
      const lines = all.filter(el => el.dataset.anim === 'line');
      const others = all.filter(el => el.dataset.anim !== 'line');
      if (others.length) animate(others, {opacity: [0, 1], y: [8, 0]}, {duration: .6, delay: stagger(.12, {start: .2}), easing: EASE});
      if (lines.length) animate(lines, {opacity: [.35, 1], y: [10, 0]}, {duration: .8, delay: stagger(.55, {start: .5}), easing: EASE});
      return;
    }

    if (recipe === 'hero') {
      animate(all, {opacity: [0, 1], y: [14, 0]}, {duration: .9, delay: stagger(.16, {start: .2}), easing: EASE});
      return;
    }

    if (recipe === 'split-statement') {
      const halves = [...slide.querySelectorAll('.half')];
      if (halves.length === 2) {
        animate(halves[0], {opacity: [0, 1], y: [18, 0]}, {duration: .7, easing: EASE});
        animate(halves[1], {opacity: [0, 1], y: [18, 0]}, {duration: .7, delay: .6, easing: EASE});
        animate(all, {opacity: [0, 1]}, {duration: .5, delay: stagger(.15, {start: .3}), easing: EASE});
        return;
      }
      animate(all, {opacity: [0, 1], y: [20, 0]}, {duration: .55, delay: stagger(.18, {start: .25}), easing: EASE});
      return;
    }

    /* 默认 cascade: 按 DOM 顺序 stagger 淡入 */
    animate(all, {opacity: [0, 1], y: [16, 0]}, {duration: .75, delay: stagger(.1, {start: .15}), easing: EASE});
  }

  /* ---------- ESC 索引视图: 1280×720 克隆页缩进卡片, 点击跳转 ---------- */
  const overview = document.createElement('div');
  overview.id = 'overview';
  overview.setAttribute('aria-label', '幻灯片索引');
  document.body.appendChild(overview);

  function buildOverview() {
    overview.innerHTML = '';
    const grid = document.createElement('div');
    grid.className = 'offline-grid';
    slides.forEach((slide, i) => {
      const card = document.createElement('div');
      card.className = 'offline-card' + (i === index ? ' current' : '');
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
      const thumb = document.createElement('div');
      thumb.className = 'offline-thumb';
      const clone = slide.cloneNode(true);
      clone.removeAttribute('id');
      clone.removeAttribute('aria-hidden');
      clone.removeAttribute('aria-current');
      thumb.appendChild(clone);
      const label = document.createElement('div');
      label.className = 'offline-num';
      label.textContent = (i + 1) + ' / ' + slides.length;
      card.appendChild(thumb);
      card.appendChild(label);
      const jump = () => { toggleOverview(false); show(i); };
      card.addEventListener('click', jump);
      card.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); jump(); }
      });
      grid.appendChild(card);
    });
    overview.appendChild(grid);
    overview.querySelectorAll('.offline-thumb').forEach(thumb => {
      const scale = (thumb.clientWidth || 220) / 1280;
      const clone = thumb.firstElementChild;
      clone.style.cssText = 'display:block;position:absolute;top:0;left:0;width:1280px;height:720px;min-width:1280px;min-height:720px;margin:0;' +
        'transform:scale(' + scale + ');transform-origin:top left;pointer-events:none';
    });
  }

  function toggleOverview(force) {
    overviewOn = typeof force === 'boolean' ? force : !overviewOn;
    if (overviewOn) {
      overview.classList.add('open');
      buildOverview();
    } else {
      overview.classList.remove('open');
      overview.innerHTML = '';
    }
  }

  /* ---------- 适配与翻页 ---------- */
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
    playSlide(index);
    window.dispatchEvent(new CustomEvent('offline-slide-change', {detail: {index, total: slides.length}}));
  }

  function forward() { if (!pipeAdvance()) show(index + 1); }

  function updateHint() {
    if (hint) hint.textContent = `← → 翻页 · 滚轮/滑动 · B ${lowPower ? '动态' : '静态'} · ESC 索引`;
  }

  function setLowPower(on, opts = {}) {
    lowPower = !!on;
    document.body.classList.toggle('low-power', lowPower);
    if (opts.persist !== false) storage.set(lowPower ? '1' : '0');
    if (lowPower && document.getAnimations) document.getAnimations().forEach(item => item.cancel());
    updateHint();
    window.dispatchEvent(new CustomEvent('ppt-low-power-change', {detail: {on: lowPower}}));
    if (currentSlide()) playSlide(index);
  }

  previous.addEventListener('click', () => show(index - 1));
  next.addEventListener('click', forward);

  addEventListener('keydown', event => {
    if (event.target.matches('input,textarea,select,[contenteditable="true"]')) return;
    if (event.key === 'Escape') { event.preventDefault(); toggleOverview(); return; }
    if (event.key && event.key.toLowerCase() === 'b' && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      setLowPower(!lowPower);
      return;
    }
    if (overviewOn) return;
    if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); forward(); }
    else if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key)) { event.preventDefault(); show(index - 1); }
    else if (event.key === 'Home') { event.preventDefault(); show(0); }
    else if (event.key === 'End') { event.preventDefault(); show(slides.length - 1); }
  });
  /* 滚轮累积翻页(150ms 空闲清零, 阈值 50), 前向先走 pipeline 推进 */
  let wheelTO = null, wheelAcc = 0;
  addEventListener('wheel', event => {
    if (overviewOn) return;
    wheelAcc += event.deltaY + event.deltaX;
    if (Math.abs(wheelAcc) > 50) {
      if (wheelAcc > 0 && pipeAdvance()) { wheelAcc = 0; }
      else { show(index + (wheelAcc > 0 ? 1 : -1)); wheelAcc = 0; }
    }
    clearTimeout(wheelTO);
    wheelTO = setTimeout(() => { wheelAcc = 0; }, 150);
  }, {passive: true});

  /* 触摸滑动(横向 >50px), 左滑先走 pipeline 推进 */
  let touchX = 0, touchY = 0;
  addEventListener('touchstart', event => {
    touchX = event.touches[0].clientX;
    touchY = event.touches[0].clientY;
  }, {passive: true});
  addEventListener('touchend', event => {
    if (overviewOn) return;
    const dx = event.changedTouches[0].clientX - touchX;
    const dy = event.changedTouches[0].clientY - touchY;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {
      if (dx < 0 && pipeAdvance()) return;
      show(index + (dx < 0 ? 1 : -1));
    }
  }, {passive: true});

  addEventListener('resize', () => {
    metrics.resizeRequests += 1;
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      fit();
      if (overviewOn) buildOverview();
    });
  }, {passive: true});

  /* 文档标题兜底: 取首页标题(正常由服务端装配时写入 <title>) */
  if (!document.title) {
    const heading = document.querySelector('.slide h1, .slide [data-element-id="title"]');
    const text = heading && heading.textContent.trim();
    document.title = text || '演示文稿';
  }

  /* 动效引导: Motion 缺失时不挂 motion-ready,所有内容保持可见 */
  document.body.classList.toggle('low-power', lowPower);
  if (motion) document.body.classList.add('motion-ready');
  else document.querySelectorAll('[data-anim]').forEach(el => { el.style.opacity = '1'; el.style.transform = 'none'; });
  updateHint();
  const requested = Number(new URLSearchParams(location.hash.slice(1)).get('slide')) - 1;
  show(Number.isInteger(requested) ? requested : 0);
})();
