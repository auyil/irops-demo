/**
 * qantas-theme.js  v4
 * ─────────────────────────────────────────────────────────────────────
 * 1. Injects Figtree font + qantas-theme.css
 * 2. Replaces nav brand with official Qantas SVG logo
 * 3. Scroll-reveal on "The Problem", "The Architect", "About" pages
 * 4. Page-load stagger on "The Solution" (fixed-height grid)
 * 5. Keeps body white across page switches
 * ─────────────────────────────────────────────────────────────────────
 */
(function () {
  'use strict';

  /* ── 1. Inject Figtree font ──────────────────────────────────────── */
  if (!document.querySelector('link[href*="Figtree"]')) {
    const lk = document.createElement('link');
    lk.rel = 'stylesheet';
    lk.href = 'https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;0,900;1,400&display=swap';
    document.head.prepend(lk);
  }

  /* ── 2. Inject theme CSS ─────────────────────────────────────────── */
  if (!document.querySelector('link[href*="qantas-theme"]')) {
    const lk = document.createElement('link');
    lk.rel = 'stylesheet';
    lk.href = 'qantas-theme.css';
    document.head.appendChild(lk);
  }

  /* ── 3. Logo replacement ─────────────────────────────────────────── */
  function patchNav() {
    const brand = document.querySelector('.nav-brand');
    if (!brand || brand.dataset.qfPatched) return;
    brand.dataset.qfPatched = '1';

    const subtitle = brand.textContent.trim();
    brand.innerHTML = '';
    brand.style.cssText = 'display:flex;align-items:center;gap:14px;';

    // Mobile logo — CSS shows this only on narrow screens
    const mobileImg = document.createElement('img');
    mobileImg.src = '/Qantas-Logo.png';
    mobileImg.alt = 'Qantas';
    mobileImg.className = 'mobile-logo';

    // Desktop logo + subtitle — CSS hides these on narrow screens
    const img = document.createElement('img');
    img.src = 'https://www.qantas.com/icons/runway_brand_logo_master_qantas_horiz.svg';
    img.alt = 'Qantas';
    img.className = 'qf-logo desktop-only';
    img.style.cssText = 'height:36px;width:auto;display:block;';
    img.onerror = function () { this.replaceWith(buildFallbackLogo()); };

    const divider = document.createElement('span');
    divider.className = '';
    divider.style.cssText = 'display:inline-block;width:1px;height:28px;background:#E5E5E5;flex-shrink:0;';

    const sub = document.createElement('span');
    sub.textContent = subtitle;
    sub.className = 'nav-brand-text';
    sub.style.cssText =
      'font-family:"Figtree",-apple-system,sans-serif;font-size:11px;' +
      'font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#999;white-space:nowrap;';

    brand.appendChild(mobileImg);
    brand.appendChild(img);
    brand.appendChild(divider);
    brand.appendChild(sub);
  }

  function buildFallbackLogo() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 160 40');
    svg.setAttribute('height', '36');
    svg.setAttribute('width', 'auto');
    svg.setAttribute('aria-label', 'Qantas');
    svg.style.cssText = 'display:block;';
    svg.innerHTML =
      '<text x="0" y="30" font-family="Figtree,-apple-system,sans-serif" ' +
      'font-size="28" font-weight="800" fill="#E8002D" letter-spacing="-0.5">QANTAS</text>';
    return svg;
  }

  function patchAvailBar() {
    const link = document.querySelector('.avail-bar a');
    if (link) link.style.color = '#7A5C00';
  }

  function enforceWhite() {
    document.body.style.background = '#FFFFFF';
    document.body.style.color = '#1A1A1A';
  }

  /* ══════════════════════════════════════════════════════════════════
     SCROLL-REVEAL
     Covers: "The Problem", "The Architect", "About"
     All are scrollable long-form pages — IntersectionObserver is ideal.
  ══════════════════════════════════════════════════════════════════ */

  const SCROLL_TARGETS = [
    /* ── Problem page ── */
    '.hero-eyebrow',
    '.hero-title',
    '.hero-body',
    '.hero-btns',
    '.stat',
    '.impact-stats',
    '.problem-card',
    '.policy-card',
    '.section-header',
    '.priority-card',
    '.phase',

    /* ── Architect page ── */
    '.arch-eyebrow',
    '.arch-h2',
    '.arch-lead',
    '.arch-section-label',
    '.pillar-card',          /* three design philosophy cards */
    '.arch-h3',
    '.arch-col-text > p',
    '.agent-table',
    '.arch-note',
    '.flow-diagram',         /* agent flow diagram */
    '.skill-card',           /* skill library cards */
    '.grule-card',           /* guardrail cards */
    '.obs-metric',           /* observability metrics */
    '.db-table-row',         /* database table list */
    '.infra-card',           /* infrastructure cards */

    /* ── About page ── */
    '.exp-item',
    '.about-left h2',
    '.about-left .role',
    '.about-left p',
    '.skill-bar',
    '.contact-link',
    '.skill-list li',
  ];

  let scrollObserver = null;

  function setupScrollReveal() {
    if (scrollObserver) scrollObserver.disconnect();

    const seen = new WeakSet();

    document.querySelectorAll(SCROLL_TARGETS.join(',')).forEach(function (el) {
      if (el.closest('nav') || seen.has(el)) return;
      seen.add(el);

      el.classList.add('qf-reveal');
      el.classList.remove('is-visible');

      /* Stagger siblings within the same parent */
      const staggered = Array.from(el.parentElement.children)
        .filter(c => c.classList.contains('qf-reveal') && !c.classList.contains('is-visible'));
      const idx = staggered.indexOf(el);
      el.classList.remove('delay-1','delay-2','delay-3','delay-4');
      if (idx > 0 && idx <= 4) el.classList.add('delay-' + idx);
    });

    scrollObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add('is-visible');
          scrollObserver.unobserve(e.target);
        }
      });
    }, { threshold: 0.10, rootMargin: '0px 0px -40px 0px' });

    document.querySelectorAll('.qf-reveal:not(.is-visible)').forEach(function (el) {
      scrollObserver.observe(el);
    });
  }

  /* ══════════════════════════════════════════════════════════════════
     PAGE SWITCH — intercept showPage / switchSec
  ══════════════════════════════════════════════════════════════════ */

  function patchSectionSwitch(fnName) {
    const orig = window[fnName];
    if (typeof orig !== 'function') return;
    window[fnName] = function (name) {
      orig(name);
      enforceWhite();
      setTimeout(setupScrollReveal, 40);
    };
  }

  /* ── Init ────────────────────────────────────────────────────────── */
  function init() {
    enforceWhite();
    patchNav();
    patchAvailBar();
    setTimeout(setupScrollReveal, 40);

    patchSectionSwitch('showPage');
    patchSectionSwitch('switchSec');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
