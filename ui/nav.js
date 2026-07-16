/* mtzRF — Menu lateral unificado + tema + controles do HackRF
   Injeta #srf-nav (sidebar) em todas as páginas.
   Expõe: window.srfNav.setWs(bool), window.srfNav.setHrf(bool)
*/
(function () {
  'use strict';

  /* ── Páginas agrupadas ───────────────────────────────────────── */
  const GRUPOS = [
    { g: 'Painel', itens: [
      { href: '/', label: 'Dashboard', icon: '⚡' },
    ]},
    { g: 'Inteligência RF', itens: [
      { href: '/analista.html',  label: 'Analista TSCM',     icon: '🕵️' },
      { href: '/sentinela.html', label: 'Sentinela',         icon: '🛡️' },
      { href: '/burst.html',     label: 'Burst Hunter',      icon: '💥' },
      { href: '/sonda.html',     label: 'Sonda',             icon: '🔎' },
      { href: '/pessoas.html',   label: 'Localizar Pessoas', icon: '🎯' },
      { href: '/ultrasom.html',  label: 'Ultrassom',         icon: '🔊' },
      { href: '/scanner.html',   label: 'Scanner',           icon: '🌌' },
    ]},
    { g: 'Decodificar', itens: [
      { href: '/radio.html',     label: 'Rádio FM',      icon: '📻' },
      { href: '/adsb.html',      label: 'Aviões ADS-B',  icon: '✈️' },
      { href: '/ism.html',       label: 'ISM / IoT',     icon: '📟' },
    ]},
    { g: 'Rede & WiFi', itens: [
      { href: '/rede.html',      label: 'Câmeras IP',    icon: '📷' },
      { href: '/wifi.html',      label: 'WiFi Red Team', icon: '🛜' },
    ]},
    { g: 'Defesa', itens: [
      { href: '/gps.html',       label: 'GPS (jamming)', icon: '🛰️' },
    ]},
    { g: 'Dados', itens: [
      { href: '/historico.html', label: 'Histórico',     icon: '🗂' },
      { href: '/alertas.html',   label: 'Alertas',       icon: '🔔' },
    ]},
    { g: 'Extras (experimental)', fechado: true, itens: [
      { href: '/doppler.html',   label: 'Doppler/Presença', icon: '🫀' },
      { href: '/health.html',    label: 'Saúde RF',      icon: '❤️' },
      { href: '/3d.html',        label: 'Ambiente 3D',   icon: '🔮' },
      { href: '/intercept.html', label: 'IMSI (WSL2)',   icon: '📱' },
      { href: '/emergencia.html',label: 'Emergência TX', icon: '🚨' },
    ]},
  ];

  /* ── Modo de HackRF por página (botão START) ─────────────────── */
  const MODOS = {
    '/': 'completo', '/index.html': 'completo',
    '/radio.html': 'radio', '/scanner.html': 'scanner', '/analista.html': 'tscm',
    '/sentinela.html': 'scanner', '/burst.html': 'tscm', '/sonda.html': 'tscm',
    '/adsb.html': 'idle', '/ism.html': 'tscm', '/rede.html': 'idle', '/wifi.html': 'idle',
    '/pessoas.html': 'idle', '/ultrasom.html': 'idle',
    '/historico.html': 'idle', '/alertas.html': 'idle', '/gps.html': 'gps',
    '/3d.html': 'completo', '/doppler.html': 'doppler', '/health.html': 'completo',
    '/intercept.html': 'imsi', '/emergencia.html': 'emergencia',
  };
  function modoAtual() {
    const p = location.pathname;
    if (p === '/' || p === '/index.html') return 'completo';
    for (const k in MODOS) { if (p.endsWith(k) && k !== '/') return MODOS[k]; }
    return 'completo';
  }

  function isActive(href) {
    const p = location.pathname;
    if (href === '/') return p === '/' || p === '/index.html';
    return p === href || p.endsWith(href);
  }

  /* ── Tema ────────────────────────────────────────────────────── */
  const LS_KEY = 'srf-theme';
  function getTheme() {
    if (document.documentElement.hasAttribute('data-force-dark')) return 'black';
    return localStorage.getItem(LS_KEY) || 'neutral';
  }
  function setTheme(t) {
    localStorage.setItem(LS_KEY, t);
    document.documentElement.setAttribute('data-theme', t === 'neutral' ? 'neutral' : '');
    const btn = document.getElementById('srfThemeBtn');
    if (btn) { btn.title = t === 'neutral' ? 'Tema escuro' : 'Tema claro'; btn.textContent = t === 'neutral' ? '◑' : '◐'; }
  }

  /* ── Grupos colapsáveis (estado salvo) ───────────────────────── */
  const grpClosed = (n) => localStorage.getItem('srf-grp-' + n) === '1';
  const setGrp = (n, closed) => localStorage.setItem('srf-grp-' + n, closed ? '1' : '0');

  /* ── Build sidebar ───────────────────────────────────────────── */
  function buildNav() {
    const grupos = GRUPOS.map(grp => {
      const ativo = grp.itens.some(p => isActive(p.href));
      const salvo = localStorage.getItem('srf-grp-' + grp.g);
      const fechadoPadrao = salvo === null ? !!grp.fechado : salvo === '1';
      const closed = !ativo && fechadoPadrao;
      const links = grp.itens.map(p =>
        `<a class="srf-link${isActive(p.href) ? ' active' : ''}" href="${p.href}"><span class="srf-icon">${p.icon}</span>${p.label}</a>`
      ).join('');
      return `<div class="srf-group${closed ? ' closed' : ''}" data-g="${grp.g}">
        <div class="srf-grp">${grp.g}<span class="srf-caret">▾</span></div>
        <div class="srf-grpitems">${links}</div></div>`;
    }).join('');

    const t = getTheme();
    return `
      <div class="srf-top">
        <a class="srf-logo" href="/">mtz<span>RF</span></a>
        <button class="srf-collapse" id="srfCollapse" title="Recolher menu">«</button>
      </div>
      <div class="srf-ctrls">
        <div class="srf-hkrow">
          <button class="srf-hk start" id="srfHkStart" title="Inicia o HackRF nesta página">▶ START</button>
          <button class="srf-hk stop"  id="srfHkStop"  title="Para tudo e libera o HackRF">■ STOP</button>
        </div>
        <div class="srf-statusrow">
          <span class="srf-ws"><span class="srf-ws-dot" id="srfWsDot"></span><span id="srfWsLbl">OFF</span></span>
          <span class="srf-hrf" id="srfHrfBadge">RF —</span>
          <span style="flex:1"></span>
          <button class="srf-theme" id="srfFsBtn" title="Tela cheia">⛶</button>
          <button class="srf-theme" id="srfThemeBtn" title="${t === 'neutral' ? 'Tema escuro' : 'Tema claro'}">${t === 'neutral' ? '◑' : '◐'}</button>
        </div>
      </div>
      <div class="srf-scroll"><nav class="srf-links">${grupos}</nav></div>`;
  }

  /* ── Inject ──────────────────────────────────────────────────── */
  function inject() {
    if (!document.querySelector('link[rel="icon"]')) {
      const ic = document.createElement('link');
      ic.rel = 'icon'; ic.type = 'image/svg+xml'; ic.href = '/favicon.svg';
      document.head.appendChild(ic);
    }

    const forceDark = document.documentElement.hasAttribute('data-force-dark');
    const saved = getTheme();
    if (forceDark) document.documentElement.removeAttribute('data-theme');
    else if (saved === 'neutral') document.documentElement.setAttribute('data-theme', 'neutral');

    // estado recolhido (salvo) ou auto-recolhe em tela estreita
    if (localStorage.getItem('srf-sb') === 'col' || window.innerWidth < 760)
      document.documentElement.classList.add('srf-collapsed');

    let nav = document.getElementById('srf-nav');
    if (!nav) { nav = document.createElement('div'); nav.id = 'srf-nav'; document.body.insertBefore(nav, document.body.firstChild); }
    nav.innerHTML = buildNav();

    // burger flutuante (reabre quando recolhido)
    let burger = document.getElementById('srf-burger');
    if (!burger) { burger = document.createElement('button'); burger.id = 'srf-burger'; burger.textContent = '☰'; burger.title = 'Abrir menu'; document.body.appendChild(burger); }
    const toggleSidebar = (col) => {
      document.documentElement.classList.toggle('srf-collapsed', col);
      localStorage.setItem('srf-sb', col ? 'col' : 'exp');
    };
    burger.onclick = () => toggleSidebar(false);
    document.getElementById('srfCollapse').onclick = () => toggleSidebar(true);

    // grupos colapsáveis
    nav.querySelectorAll('.srf-grp').forEach(h => {
      h.addEventListener('click', () => {
        const grp = h.parentElement;
        grp.classList.toggle('closed');
        setGrp(grp.getAttribute('data-g'), grp.classList.contains('closed'));
      });
    });

    // tema
    const themeBtn = document.getElementById('srfThemeBtn');
    if (forceDark) themeBtn.style.display = 'none';
    else themeBtn.addEventListener('click', () => setTheme(getTheme() === 'neutral' ? 'black' : 'neutral'));

    // tela cheia
    const fsBtn = document.getElementById('srfFsBtn');
    if (fsBtn) {
      fsBtn.addEventListener('click', () => {
        if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
        else document.documentElement.requestFullscreen().catch(() => {});
      });
      document.addEventListener('fullscreenchange', () => {
        fsBtn.textContent = document.fullscreenElement ? '🗗' : '⛶';
      });
    }

    // contador de alertas
    const alLink = document.querySelector('.srf-link[href="/alertas.html"]');
    if (alLink) {
      const upd = async () => {
        try {
          const d = await (await fetch('/api/alertas')).json();
          const n = (d.alertas || []).length;
          const altos = (d.alertas || []).filter(a => a.nivel >= 2).length;
          let b = alLink.querySelector('.srf-albadge');
          if (n > 0) {
            if (!b) { b = document.createElement('span'); b.className = 'srf-albadge'; alLink.appendChild(b); }
            b.textContent = n;
            b.style.cssText = 'margin-left:auto;padding:0 6px;border-radius:8px;font-size:9px;font-weight:700;background:' + (altos ? 'var(--r)' : 'var(--g3)') + ';color:#fff;';
          } else if (b) { b.remove(); }
        } catch {}
      };
      upd(); setInterval(upd, 15000);
    }

    // START / STOP
    const btnStart = document.getElementById('srfHkStart');
    const btnStop  = document.getElementById('srfHkStop');
    btnStart.addEventListener('click', async () => {
      btnStart.disabled = true; btnStart.textContent = '⏳';
      try {
        const r = await fetch('/api/hackrf/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ modo: modoAtual() }) });
        const d = await r.json(); if (!r.ok) throw new Error(d.detail || 'erro');
        btnStart.style.display = 'none'; btnStart.classList.add('ativo'); btnStop.style.display = ''; window.srfNav.setHrf(true);
      } catch (e) { alert('Erro ao iniciar HackRF: ' + e.message); }
      finally { btnStart.disabled = false; btnStart.textContent = '▶ START'; }
    });
    btnStop.addEventListener('click', async () => {
      btnStop.disabled = true; btnStop.textContent = '⏳';
      try { await fetch('/api/hackrf/stop', { method: 'POST' }); } catch (e) {}
      btnStop.style.display = 'none'; btnStop.disabled = false; btnStop.textContent = '■ STOP';
      btnStart.style.display = ''; btnStart.classList.remove('ativo'); window.srfNav.setHrf(false);
    });
  }

  // Escapa dados de terceiros (SSID, hostname, callsign, campos do portal…) antes
  // de inserir em innerHTML — evita XSS a partir de nomes controlados por RF/rede.
  window.esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  window.srfNav = {
    setWs(on) {
      const dot = document.getElementById('srfWsDot'), lbl = document.getElementById('srfWsLbl');
      if (dot) dot.className = 'srf-ws-dot' + (on ? ' on' : '');
      if (lbl) lbl.textContent = on ? 'WS' : 'OFF';
    },
    setHrf(on) {
      const b = document.getElementById('srfHrfBadge'); if (!b) return;
      b.textContent = on ? 'RF: ATIVO' : 'RF —'; b.className = 'srf-hrf' + (on ? ' on' : '');
    },
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', inject);
  else inject();
})();
