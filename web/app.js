/* ProjectCommit -- interface.
   Sem framework e sem build: o pacote PyInstaller so' copia web/ como esta'. */

'use strict';

const AIS = {
  claude:      { label: 'Claude',      color: 'var(--claude)' },
  codex:       { label: 'Codex',       color: 'var(--codex)' },
  antigravity: { label: 'Antigravity', color: 'var(--antigravity)' },
};

/* O git também escreve na linha do tempo, mas NÃO é uma IA: fica fora de AIS
   para não virar uma pastilha no Radar (lá o filtro é "qual IA tocou o
   projeto"). O cinza é o mesmo que a linha de vida já usa para commit. */
const AUTORES = Object.assign({}, AIS, {
  git: { label: 'Commits', color: 'var(--text-2)' },
});

const state = {
  view: 'radar',
  boot: null,
  projects: [],
  radar: null,
  filterAi: 'todas',
  filterText: '',
  filtroImagem: 'todas',
  filtroArquivo: 'todos',
  filtroFamilia: 'todas',
  ordemArquivo: 'editados',
  buscaQ: '',
  busca: { ai: 'todas', kind: 'tudo', projeto: '', dias: '' },
  buscaFacetas: null,
  buscaItens: [],
  openProject: null,
  drawerTab: 'sessoes',
  arqSel: null,
  dossie: null,
  focoAnterior: null,
  scanTimer: null,
  liveTimer: null,
  autoScanTimer: null,
  transcript: null,
};

/* ---------- utilidades ---------- */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* Cards e linhas são <article>/<div> com cursor:pointer e onclick — sem role,
   sem tabindex. A página inteira tinha 18 elementos focáveis e NENHUM abria um
   projeto: sem mouse o painel não navegava. Este é o único lugar que liga uma
   ação a um alvo, para que ninguém volte a escrever `el.onclick =` solto.

   Aplicar só onde a ação é "abrir": uma linha que contém caixa de seleção não é
   um botão, e dar role="button" a ela mentiria para o leitor de tela. */
function ligarAlvo(el, acao, rotulo) {
  if (!el) return;
  el.setAttribute('role', 'button');
  el.setAttribute('tabindex', '0');
  if (rotulo) el.setAttribute('aria-label', rotulo);
  el.onclick = acao;
  el.onkeydown = ev => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); acao(ev); }
  };
}

/** Liga todos os alvos de um seletor de uma vez. */
function ligarAlvos(sel, acao, rotulo) {
  $$(sel).forEach(el => ligarAlvo(el, ev => acao(el, ev), rotulo && rotulo(el)));
}

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtAgo(epoch) {
  if (!epoch) return 'nunca';
  const secs = Date.now() / 1000 - epoch;
  if (secs < 60) return 'agora';
  if (secs < 3600) return `há ${Math.floor(secs / 60)} min`;
  if (secs < 86400) return `há ${Math.floor(secs / 3600)} h`;
  const days = Math.floor(secs / 86400);
  if (days === 1) return 'ontem';
  if (days < 30) return `há ${days} dias`;
  if (days < 365) return `há ${Math.floor(days / 30)} meses`;
  return `há ${Math.floor(days / 365)} anos`;
}

function fmtDate(epoch, withTime) {
  if (!epoch) return '—';
  const d = new Date(epoch * 1000);
  const opts = withTime
    ? { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { day: '2-digit', month: 'short', year: 'numeric' };
  return d.toLocaleString('pt-BR', opts);
}

function fmtDayLabel(epoch) {
  const d = new Date(epoch * 1000);
  const today = new Date();
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, today)) return 'Hoje';
  const yest = new Date(today.getTime() - 86400000);
  if (same(d, yest)) return 'Ontem';
  return d.toLocaleDateString('pt-BR', { weekday: 'long', day: '2-digit', month: 'long' });
}

/* Um travessao no lugar de 0 quando a fonte simplesmente nao informa.
   O Antigravity guarda tudo em protobuf e nao expoe consumo -- mostrar "0"
   ao lado dos 368 mi do Claude faria parecer que ele nao gastou nada. */
function fmtTokens(n, ai) {
  if (!n && ai === 'antigravity') return '<span title="O formato do Antigravity não expõe consumo de tokens" style="color:var(--faint)">—</span>';
  return fmtNum(n);
}

function fmtNum(n) {
  n = Number(n || 0);
  if (n >= 1e9) return (n / 1e9).toFixed(1).replace('.', ',') + ' bi';
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace('.', ',') + ' mi';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + ' mil';
  return String(n);
}

function fmtBytes(n) {
  n = Number(n || 0);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1).replace('.', ',')) + ' ' + units[i];
}

function shortModel(model) {
  if (!model) return '';
  return String(model)
    .replace(/^claude-/, '')
    .replace(/-\d{8}$/, '')
    .replace(/^gpt-/, 'GPT-');
}

function toast(message, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, options) {
  const res = await fetch('/api/' + path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).error || detail; } catch (e) { /* corpo nao-JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

/* Faisca de atividade: 30 dias, um tracinho por dia. */
function sparkline(values, color) {
  const w = 62, h = 18, n = values.length;
  const max = Math.max(1, ...values);
  const bw = w / n;
  const bars = values.map((v, i) => {
    if (!v) return '';
    const bh = Math.max(1.5, (v / max) * h);
    return `<rect x="${(i * bw).toFixed(2)}" y="${(h - bh).toFixed(2)}" width="${(bw - .8).toFixed(2)}"
             height="${bh.toFixed(2)}" rx="1" fill="${color}" opacity="${(0.35 + 0.65 * (v / max)).toFixed(2)}"/>`;
  }).join('');
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">${bars}</svg>`;
}

function aiDots(ais) {
  if (!ais || !ais.length) return '';
  return `<span class="ai-dots">${ais.map(a =>
    `<span class="ai-dot ${esc(a)}" title="${esc(AIS[a] ? AIS[a].label : a)}"></span>`).join('')}</span>`;
}

/* ---------- roteador ---------- */

const VIEWS = {};

const TELAS = ['radar', 'timeline', 'busca', 'consumo', 'faxina', 'config'];

function navigate() {
  const hash = location.hash.replace(/^#\//, '') || ultimaTela();
  const [name, arg, arg2] = hash.split('/');

  // #/projeto/<id>/<aba> abre a gaveta direto -- serve para voltar a um projeto
  // sem recomeçar do Radar, e para conferir uma aba numa captura.
  if (name === 'projeto' && arg) {
    const jaAberto = state.openProject && state.openProject.id === Number(arg);
    // A gaveta agora escreve na URL ao trocar de aba, e isso dispara hashchange
    // de volta aqui. Sem esta saída, cada clique em aba recarregaria o projeto
    // inteiro do servidor -- e a troca de aba, que é instantânea, passaria a
    // custar uma ida à rede.
    if (jaAberto && $('#drawer') && !$('#drawer').hidden) {
      if (arg2 && state.drawerTab !== arg2) { state.drawerTab = arg2; drawDrawer(); }
      return;
    }
    if (!state.projects.length) {
      loadRadar().then(() => { drawRadar(); openProject(Number(arg), arg2); });
    } else {
      openProject(Number(arg), arg2);
    }
    return;
  }
  // Saiu de um projeto (Voltar, ou clique na barra lateral): a gaveta tem que
  // acompanhar, senão ela fica aberta por cima de outra tela.
  if (state.openProject) closeDrawer();
  state.view = VIEWS[name] ? name : 'radar';
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.view === state.view));
  try { localStorage.setItem('pc-tela', state.view); } catch (e) { /* sem storage */ }
  VIEWS[state.view](arg);
}

/** Abre onde você parou. Voltar sempre ao Radar custa um clique a cada abertura. */
function ultimaTela() {
  try {
    const t = localStorage.getItem('pc-tela');
    return TELAS.includes(t) ? t : 'radar';
  } catch (e) { return 'radar'; }
}

function render(html) {
  $('#content').innerHTML = html;
}

/* ---------- Radar ---------- */

VIEWS.radar = async function () {
  if (!state.projects.length) render(skeleton());
  await loadRadar();
  drawRadar();
};

async function loadRadar() {
  try {
    const data = await api('radar');
    state.projects = data.projects;
    state.radar = data.radar;
    drawLiveSidebar();
  } catch (err) {
    toast('Falha ao carregar o radar: ' + err.message, 'err');
  }
}

/* Um filtro só para o Radar e para a linha do tempo — de propósito, para que
   escolher "Codex" numa tela siga valendo na outra. Mas cada tela entende um
   conjunto diferente de valores: `git` (Commits) só existe na linha do tempo e
   `com-ia` só no Radar. Cada uma sanitiza o que não entende para "tudo", em vez
   de filtrar por um valor impossível e mostrar a tela vazia — foi o que a
   pastilha Commits fez com os 47 projetos do Radar antes desta regra. */
const COM_IA = 'com-ia';

function filtroDeIa() {
  if (state.filterAi === COM_IA) return COM_IA;
  return AIS[state.filterAi] ? state.filterAi : 'todas';
}

/** Autor que a linha do tempo entende: uma das IAs ou `git`. */
function filtroDeAutor() {
  return AUTORES[state.filterAi] ? state.filterAi : 'todas';
}

function visibleProjects() {
  const text = state.filterText.trim().toLowerCase();
  const ia = filtroDeIa();
  return state.projects.filter(p => {
    if (ia === COM_IA && !(p.ais || []).length) return false;
    if (ia !== 'todas' && ia !== COM_IA && !(p.ais || []).includes(ia)) return false;
    if (text && !(p.name || '').toLowerCase().includes(text)
             && !(p.path || '').toLowerCase().includes(text)) return false;
    return true;
  });
}

function drawRadar() {
  const items = visibleProjects();
  const live = (state.radar && state.radar.sessions) || [];
  const withAi = state.projects.filter(p => (p.ais || []).length).length;
  const semGit = state.boot && state.boot.git_available === false;

  const counts = { claude: 0, codex: 0, antigravity: 0 };
  state.projects.forEach(p => (p.ais || []).forEach(a => { if (a in counts) counts[a] += 1; }));

  render(`
    ${semGit ? `<div class="note" style="margin-bottom:18px">
      <strong>Git não encontrado no PATH.</strong> As informações de branch, commits e
      arquivos pendentes ficaram congeladas na última leitura boa — elas não foram
      apagadas. Instale o Git ou abra o programa por um terminal que o enxergue.
    </div>` : ''}
    <div class="page-head">
      <div>
        <h1 class="page-title">Radar</h1>
        <p class="page-sub">${state.projects.length} projetos &middot; ${withAi} tocados por alguma IA</p>
      </div>
      <div class="spacer"></div>
      <input id="filtro-projeto" type="search" placeholder="Filtrar projeto&hellip;"
             value="${esc(state.filterText)}" autocomplete="off" spellcheck="false"
             style="height:28px;padding:0 11px;background:var(--surface);border:1px solid transparent;
                    border-radius:999px;color:var(--text);font:inherit;font-size:12.5px;outline:none;width:170px">
      <div class="filters">
        <button class="chip ${filtroDeIa() === 'todas' ? 'on' : ''}" data-ai="todas">Todas
          <span style="color:var(--faint)">${state.projects.length}</span></button>
        <button class="chip ${filtroDeIa() === COM_IA ? 'on' : ''}" data-ai="${COM_IA}"
          title="Esconde os ${state.projects.length - withAi} projetos que nenhuma IA tocou">Com IA
          <span style="color:var(--faint)">${withAi}</span></button>
        ${Object.keys(AIS).map(a => `
          <button class="chip ${filtroDeIa() === a ? 'on' : ''}" data-ai="${a}">
            <span class="cdot" style="background:${AIS[a].color}"></span>${AIS[a].label}
            <span style="color:var(--faint)">${counts[a]}</span>
          </button>`).join('')}
      </div>
    </div>
    ${items.length ? `<div class="grid">${items.map(projectCard).join('')}</div>` : emptyState()}
  `);

  const filtro = $('#filtro-projeto');
  filtro.oninput = () => {
    state.filterText = filtro.value;
    const foco = document.activeElement === filtro;
    const pos = filtro.selectionStart;
    drawRadar();
    if (foco) { const novo = $('#filtro-projeto'); novo.focus(); novo.setSelectionRange(pos, pos); }
  };
  $$('.chip[data-ai]').forEach(el => el.onclick = () => {
    state.filterAi = el.dataset.ai;
    drawRadar();
  });
  ligarAlvos('.card[data-id]', el => openProject(Number(el.dataset.id)),
    el => 'Abrir ' + (el.querySelector('.card-name') || {}).textContent);
}

/* A faixa "Agora" no topo do Radar foi removida: ela mostrava exatamente o que a
   barra lateral já mostra em "ACONTECENDO AGORA", e cobrava 52px de altura numa
   janela de 694. A da barra lateral venceu porque vale em TODAS as telas, não só
   no Radar, e não disputa espaço com o conteúdo. O selo "ao vivo" continua no
   card do projeto. Ver drawLiveSidebar(). */

function projectCard(p) {
  const isLive = !!p.live;
  let git;
  if (!p.on_disk) {
    // Uma IA trabalhou aqui e a pasta nao existe mais -- vale dizer isso alto.
    git = `<span class="tag dirty" title="Uma IA trabalhou neste caminho, mas a pasta n&atilde;o existe mais">sumiu do disco</span>`;
  } else if (p.has_git) {
    // Uma tag âmbar só. Antes "N sem commit" e o alerta apareciam lado a lado,
    // duas cores de aviso competindo pela mesma atenção no mesmo card.
    const pend = [];
    if (p.dirty_count) pend.push(`${p.dirty_count} sem commit`);
    if (p.alerta && p.alerta.dias >= 1) pend.push(`há ${p.alerta.dias}d`);
    else if (p.alerta) pend.push('IA mexeu depois');
    git = `<span class="tag mono">${esc(p.branch || '?')}</span>
       ${p.commit_count !== null ? `<span class="tag">${p.commit_count} commits</span>` : ''}
       ${pend.length ? `<span class="tag dirty"
          title="${esc(p.alerta ? p.alerta.texto : 'alterações não commitadas')}">${pend.join(' · ')}</span>` : ''}`;
  } else if (p.alerta) {
    // Sem git E com trabalho de IA dentro: o caso mais exposto do painel, e até
    // esta versão o único que não avisava nada. O selo neutro "sem git" dizia
    // que faltava repositório, nunca que havia trabalho lá dentro.
    git = `<span class="tag nogit">sem git</span>
      <span class="tag dirty" title="${esc(p.alerta.texto)}">
        ${p.alerta.arquivos} da IA sem rede</span>`;
  } else {
    git = `<span class="tag nogit">sem git</span>`;
  }

  const lastAi = p.last_ai && AIS[p.last_ai] ? AIS[p.last_ai].label : '';
  const activity = p.last_activity || p.fs_mtime;
  const sparkColor = p.last_ai && AIS[p.last_ai] ? AIS[p.last_ai].color : 'var(--muted)';

  return `
  <article class="card ${isLive ? 'is-live' : ''} ${p.on_disk ? '' : 'off-disk'}" data-id="${p.id}">
    <div class="card-head">
      <div class="card-name" title="${esc(p.name)}">${esc(p.name)}${p.discovered
        ? '<span style="color:var(--faint)" title="Descoberto pelo cwd de uma IA, fora das pastas configuradas"> *</span>' : ''}</div>
      ${isLive ? '<span class="live-badge"><span class="pulse"></span>ao vivo</span>' : aiDots(p.ais)}
    </div>
    <div class="card-row">
      <span class="tag onde" title="${esc(p.path)}">${esc(ondeFica(p))}</span>${git}
    </div>
    <div class="card-row">
      ${p.session_count ? `<span>${p.session_count} ${p.session_count === 1 ? 'sessão' : 'sessões'}</span>` : '<span style="color:var(--faint)">sem sessão</span>'}
      ${p.tok_total
        ? `<span class="sep">&middot;</span><span>${fmtNum(p.tok_total)} tokens</span>`
        : (p.ais || []).length === 1 && p.ais[0] === 'antigravity'
          ? `<span class="sep">&middot;</span><span style="color:var(--faint)" title="O formato do Antigravity não expõe consumo">tokens n/d</span>` : ''}
      <span class="grow"></span>
      ${sparkline(p.spark || [], sparkColor)}
    </div>
    <div class="card-foot">
      <span class="last">${activity ? `${esc(fmtAgo(activity))}${lastAi ? ' &middot; ' + esc(lastAi) : ''}${p.last_model ? ' ' + esc(shortModel(p.last_model)) : ''}` : 'sem atividade registrada'}</span>
    </div>
  </article>`;
}

/* O caminho inteiro ocupava uma linha própria em cada card e era quase todo
   prefixo repetido — a mesma pasta-mãe em metade deles. O que
   distingue um projeto do outro é a PASTA que o contém, então é só ela que vai
   à tela, como selo ao lado do git. O caminho completo fica no title. */
function ondeFica(p) {
  const partes = (p.path || '').split(/[\\/]/).filter(Boolean);
  // Último segmento é o próprio nome do projeto, que já está no título.
  const pai = partes.length > 1 ? partes[partes.length - 2] : null;
  // Pai que é só a letra do drive não vira "…/F:" — vira "F:\", que se lê.
  if (!pai || /^[A-Za-z]:$/.test(pai)) return (pai || partes[0] || '') + '\\';
  return '…/' + pai;
}

function emptyState() {
  return `<div class="empty">
    <h3>Nenhum projeto para mostrar</h3>
    <p>Ajuste o filtro, ou revise as pastas varridas em Configura&ccedil;&otilde;es.</p>
    <button class="btn primary" onclick="location.hash='#/config'">Abrir configura&ccedil;&otilde;es</button>
  </div>`;
}

function skeleton() {
  return `<div class="skeleton-page"><div class="sk-line w40"></div>
    <div class="sk-grid">${'<div class="sk-card"></div>'.repeat(6)}</div></div>`;
}

/* ---------- Linha do tempo ---------- */

VIEWS.timeline = async function () {
  render(`
    <div class="page-head">
      <div>
        <h1 class="page-title">Linha do tempo</h1>
        <p class="page-sub">Tudo o que as tr&ecirc;s IAs fizeram, em ordem</p>
      </div>
      <div class="spacer"></div>
      <div class="filters" id="tl-filters">
        <button class="chip ${filtroDeAutor() === 'todas' ? 'on' : ''}" data-ai="todas">Tudo</button>
        ${Object.keys(AUTORES).map(a => `
          <button class="chip ${filtroDeAutor() === a ? 'on' : ''}" data-ai="${a}">
            <span class="cdot" style="background:${AUTORES[a].color}"></span>${AUTORES[a].label}
          </button>`).join('')}
      </div>
    </div>
    <div id="tl-body"><div class="sk-line w40"></div></div>
  `);
  $$('#tl-filters .chip').forEach(el => el.onclick = () => { state.filterAi = el.dataset.ai; VIEWS.timeline(); });
  await drawTimeline();
};

const TL_PAGINA = 150;

async function drawTimeline(anexar) {
  let data;
  const carregados = anexar ? (state.tlEvents || []).length : 0;
  try {
    const qs = new URLSearchParams({ limit: TL_PAGINA, offset: carregados });
    const autor = filtroDeAutor();
    if (autor !== 'todas') qs.set('ai', autor);
    data = await api('timeline?' + qs);
  } catch (err) {
    $('#tl-body').innerHTML = `<div class="empty"><h3>N&atilde;o deu para carregar</h3><p>${esc(err.message)}</p></div>`;
    return;
  }
  state.tlEvents = anexar ? (state.tlEvents || []).concat(data.events) : data.events;
  state.tlFim = data.events.length < TL_PAGINA;

  if (!state.tlEvents.length) {
    $('#tl-body').innerHTML = `<div class="empty"><h3>Nada por aqui ainda</h3>
      <p>Rode uma atualiza&ccedil;&atilde;o do &iacute;ndice para trazer o hist&oacute;rico.</p></div>`;
    return;
  }

  let html = '<div class="timeline">';
  let lastDay = null;
  state.tlEvents.forEach(e => {
    const day = new Date(e.ts * 1000).toDateString();
    if (day !== lastDay) {
      lastDay = day;
      html += `<div class="tl-day">${esc(fmtDayLabel(e.ts))}</div>`;
    }
    const hora = new Date(e.ts * 1000).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
    const commit = e.kind === 'commit';
    html += `<div class="tl-item ${esc(e.ai)}${commit ? ' commit' : ''}" ${e.project_id ? `data-id="${e.project_id}"` : ''}>
      <div class="tl-meta">
        <strong style="color:var(--text-2)">${esc(e.project_name || 'sem projeto')}</strong>
        <span>${commit ? 'commit' : esc(AUTORES[e.ai] ? AUTORES[e.ai].label : e.ai)}</span>
        ${e.model ? `<span>${esc(shortModel(e.model))}</span>` : ''}
        <span>${hora}</span>
      </div>
      <div class="tl-text">${esc(e.text || e.session_title || '')}</div>
    </div>`;
  });
  html += '</div>';
  // Sem isto a linha do tempo parava calada na primeira pagina, e nada na tela
  // dizia que havia mais historico atras.
  html += state.tlFim
    ? `<p style="text-align:center;color:var(--faint);font-size:12px;margin:24px 0">
         fim do hist&oacute;rico &middot; ${state.tlEvents.length} mensagens</p>`
    : `<div style="text-align:center;margin:24px 0">
         <button class="btn" id="tl-mais">Carregar mais</button>
         <div style="font-size:11.5px;color:var(--faint);margin-top:7px">
           ${state.tlEvents.length} carregadas</div>
       </div>`;

  $('#tl-body').innerHTML = html;
  ligarAlvos('.tl-item[data-id]', el => openProject(Number(el.dataset.id)),
    el => 'Abrir ' + ((el.querySelector('strong') || {}).textContent || 'projeto'));
  const mais = $('#tl-mais');
  if (mais) mais.onclick = async () => {
    mais.disabled = true;
    mais.textContent = 'Carregando…';
    await drawTimeline(true);
  };
}

/* ---------- Busca ---------- */

const KIND = { session: 'conversa', event: 'você pediu', msg: 'a IA respondeu', artifact: 'documento' };
const KIND_ORDEM = ['event', 'msg', 'artifact', 'session'];
const PERIODOS = [['', 'Sempre'], ['7', '7 dias'], ['30', '30 dias'], ['90', '90 dias']];

VIEWS.busca = async function (queryFromUrl) {
  const query = queryFromUrl ? decodeURIComponent(queryFromUrl) : ($('#global-search').value || '');
  // Termo novo zera os filtros: manter "só Codex" de uma busca anterior faria a
  // seguinte parecer vazia sem explicação.
  if (query !== state.buscaQ) state.busca = { ai: 'todas', kind: 'tudo', projeto: '', dias: '' };
  state.buscaQ = query;
  render(`
    <div class="page-head">
      <div>
        <h1 class="page-title">Busca</h1>
        <p class="page-sub" id="busca-sub">Conversas do Claude, threads do Codex e planos do Antigravity</p>
      </div>
    </div>
    <div id="busca-filtros"></div>
    <div id="busca-body"></div>
  `);
  if (!query || query.trim().length < 2) {
    $('#busca-body').innerHTML = `<div class="empty"><h3>Digite pelo menos 2 letras</h3>
      <p>Use a caixa no topo. Ex.: <em>encoder de vinil</em>, <em>ISO</em>, <em>supabase</em>.</p></div>`;
    return;
  }
  await rodarBusca();
};

const BUSCA_PAGINA = 60;

async function rodarBusca(anexar) {
  const f = state.busca || (state.busca = { ai: 'todas', kind: 'tudo', projeto: '', dias: '' });
  const carregados = anexar ? (state.buscaItens || []).length : 0;
  if (!anexar) {
    state.buscaItens = [];
    $('#busca-body').innerHTML = '<div class="sk-line w40"></div>';
  }
  const qs = new URLSearchParams({ q: state.buscaQ, limit: BUSCA_PAGINA, offset: carregados });
  if (f.ai !== 'todas') qs.set('ai', f.ai);
  if (f.kind !== 'tudo') qs.set('kind', f.kind);
  if (f.projeto) qs.set('project_id', f.projeto);
  if (f.dias) qs.set('dias', f.dias);

  let data;
  try {
    data = await api('search?' + qs);
  } catch (err) {
    $('#busca-body').innerHTML = `<div class="empty"><h3>Busca falhou</h3><p>${esc(err.message)}</p></div>`;
    return;
  }
  state.buscaItens = (state.buscaItens || []).concat(data.results);
  state.buscaFacetas = data.facetas;
  desenharFiltrosBusca(data);

  const filtrando = f.ai !== 'todas' || f.kind !== 'tudo' || !!f.projeto || !!f.dias;
  const juntados = (data.brutos || 0) - (data.achados || 0);
  $('#busca-sub').innerHTML = data.total
    ? `<strong>${data.total}</strong> acerto${data.total === 1 ? '' : 's'} no índice${
        filtrando ? ` &middot; <strong>${data.achados}</strong> com os filtros` : ''}${
        juntados > 0 ? ` &middot; ${juntados} quase repetido${juntados === 1 ? '' : 's'} agrupado${juntados === 1 ? '' : 's'}` : ''}${
        data.truncado ? ` &middot; <span style="color:var(--warn)">os filtros valem sobre os
          ${data.teto || 1200} mais relevantes</span>` : ''}`
    : 'Conversas do Claude, threads do Codex e planos do Antigravity';

  if (!state.buscaItens.length) {
    $('#busca-body').innerHTML = data.total
      ? `<div class="empty"><h3>Nada com esses filtros</h3>
           <p>Há ${data.total} acerto${data.total === 1 ? '' : 's'} para
              &ldquo;${esc(state.buscaQ)}&rdquo;, mas nenhum sobrevive ao recorte atual.</p></div>`
      : `<div class="empty"><h3>Nada encontrado para &ldquo;${esc(state.buscaQ)}&rdquo;</h3>
           <p>Tente outra palavra, ou atualize o &iacute;ndice.</p></div>`;
    return;
  }

  const fim = state.buscaItens.length >= data.achados;
  $('#busca-body').innerHTML =
    `<div class="list">${state.buscaItens.map((r, i) => linhaBusca(r, i)).join('')}</div>` +
    (fim
      ? `<p style="text-align:center;color:var(--faint);font-size:12px;margin:22px 0">
           fim dos resultados &middot; ${state.buscaItens.length} mostrado${state.buscaItens.length === 1 ? '' : 's'}</p>`
      : `<div style="text-align:center;margin:22px 0">
           <button class="btn" id="busca-mais">Carregar mais</button>
           <div style="font-size:11.5px;color:var(--faint);margin-top:7px">
             ${state.buscaItens.length} de ${data.achados}</div>
         </div>`);

  ligarAlvos('#busca-body .row[data-abrir]', async el => {
    const sid = el.dataset.sid;
    if (el.dataset.id) await openProject(Number(el.dataset.id));
    // Um trecho de resposta só faz sentido dentro da conversa: abrir o projeto
    // e parar na aba Sessões obrigaria a caçar de novo o que a busca já achou.
    if (sid) openTranscript(sid, undefined, undefined, el.dataset.at);
  }, el => 'Abrir ' + ((el.querySelector('strong') || {}).textContent || 'resultado'));

  $$('#busca-body [data-expandir]').forEach(el => el.onclick = ev => {
    ev.stopPropagation();
    const cx = $('#semelhantes-' + el.dataset.expandir);
    if (cx) { cx.hidden = !cx.hidden; el.textContent = cx.hidden ? el.dataset.rotulo : 'ocultar'; }
  });

  const mais = $('#busca-mais');
  if (mais) mais.onclick = async () => {
    mais.disabled = true; mais.textContent = 'Carregando…';
    await rodarBusca(true);
  };
}

function linhaBusca(r, i, dentroDeGrupo) {
  const semelhantes = r.semelhantes || [];
  return `
    <div class="row" data-abrir="1" ${r.project_id ? `data-id="${r.project_id}"` : ''}
         ${r.kind === 'msg' ? `data-sid="${esc(r.session_id)}" data-at="${esc(r.at)}"` : ''}
         ${dentroDeGrupo ? 'style="padding-left:30px"' : ''}>
      <span class="ai-dot ${esc(r.ai || '')}" style="flex:0 0 7px"></span>
      <div class="grow">
        <div class="ellip"><strong>${esc(r.project_name || r.title || 'sem projeto')}</strong>
          <span style="color:var(--faint);font-size:12px"> &middot; ${esc(KIND[r.kind] || r.kind)}</span></div>
        <div style="font-size:12.5px;color:var(--muted)">${highlight(r.snippet)}</div>
      </div>
      ${semelhantes.length
        ? `<button class="tag" data-expandir="${i}" data-rotulo="+${semelhantes.length} parecido${semelhantes.length === 1 ? '' : 's'}"
             style="flex:0 0 auto;cursor:pointer;border:none"
             title="Mesmo projeto e mesmo texto, mudando só números e horário"
             >+${semelhantes.length} parecido${semelhantes.length === 1 ? '' : 's'}</button>` : ''}
      <span class="num">${esc(fmtDate(r.ended_at))}</span>
    </div>` + (semelhantes.length
      ? `<div id="semelhantes-${i}" hidden>${semelhantes.map((s, j) => linhaBusca(s, i + '_' + j, true)).join('')}</div>`
      : '');
}

function desenharFiltrosBusca(data) {
  const f = state.busca, fa = data.facetas || { ai: {}, kind: {}, projetos: [] };
  const conta = n => n ? `<span style="color:var(--faint)">${n}</span>` : '';
  $('#busca-filtros').innerHTML = `
    <div class="filters" style="margin-bottom:8px">
      <button class="chip ${f.kind === 'tudo' ? 'on' : ''}" data-bk="tudo">Tudo</button>
      ${KIND_ORDEM.filter(k => fa.kind[k]).map(k => `
        <button class="chip ${f.kind === k ? 'on' : ''}" data-bk="${k}">${KIND[k]} ${conta(fa.kind[k])}</button>`).join('')}
    </div>
    <div class="filters" style="margin-bottom:14px">
      <button class="chip ${f.ai === 'todas' ? 'on' : ''}" data-bai="todas">Todas as IAs</button>
      ${Object.keys(AIS).filter(a => fa.ai[a]).map(a => `
        <button class="chip ${f.ai === a ? 'on' : ''}" data-bai="${a}">
          <span class="cdot" style="background:${AIS[a].color}"></span>${AIS[a].label} ${conta(fa.ai[a])}
        </button>`).join('')}
      <select id="busca-proj" class="chip" style="padding:0 8px;height:26px;border:none">
        <option value="">Todos os projetos</option>
        ${(fa.projetos || []).map(p => `
          <option value="${p.id}" ${String(f.projeto) === String(p.id) ? 'selected' : ''}>${esc(p.name)} (${p.n})</option>`).join('')}
      </select>
      <select id="busca-dias" class="chip" style="padding:0 8px;height:26px;border:none">
        ${PERIODOS.map(([v, r]) => `<option value="${v}" ${f.dias === v ? 'selected' : ''}>${r}</option>`).join('')}
      </select>
    </div>`;
  $$('#busca-filtros [data-bk]').forEach(el => el.onclick = () => { f.kind = el.dataset.bk; rodarBusca(); });
  $$('#busca-filtros [data-bai]').forEach(el => el.onclick = () => { f.ai = el.dataset.bai; rodarBusca(); });
  $('#busca-proj').onchange = ev => { f.projeto = ev.target.value; rodarBusca(); };
  $('#busca-dias').onchange = ev => { f.dias = ev.target.value; rodarBusca(); };
}

/* O snippet do FTS5 vem com marcadores << >>; escapamos antes de virar <mark>. */
function highlight(snippet) {
  return esc(snippet || '')
    .replace(/&lt;&lt;/g, '<mark style="background:color-mix(in srgb,var(--claude) 30%,transparent);color:inherit;border-radius:3px;padding:0 2px">')
    .replace(/&gt;&gt;/g, '</mark>');
}

/* ---------- Custo ---------- */



/* ---------- Consumo (gráficos) ---------- */

/** Alguns títulos de thread do Codex são, na verdade, o prompt de sistema que a
 *  ferramenta injeta ("The following is the Codex agent…"). Usá-los como rótulo
 *  deixava várias barras com o mesmo texto e sem sentido. */
const LIXO_TITULO = /^(the following is|you are|<|system:|# )/i;

/* UUID cru como rótulo não diz nada — e era o que aparecia em 3 de cada 8 linhas
   da lista de sessões, porque o `native_id` era o último recurso. Ele continua
   no `title=` do elemento, para quem precisar do id. */
const SO_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function tituloUsavel(t) {
  const s = (t || '').trim();
  if (!s || SO_UUID.test(s) || LIXO_TITULO.test(s)) return null;
  return s;
}

/** Rótulo de uma conversa, em qualquer lugar da interface.
 *  Era usada só no gráfico do Codex; os outros três pontos montavam o texto à
 *  mão com `title || first_prompt || native_id` — e o `first_prompt` entrava sem
 *  filtro nenhum, que é por onde o prompt de sistema do Codex chegava à tela. */
function rotuloConversa(t, corte = 34) {
  const bom = tituloUsavel(t.title) || tituloUsavel(t.first_prompt);
  if (bom) return bom.length > corte ? bom.slice(0, corte - 1) + '…' : bom;
  /* Sem nada legível, o rótulo é a única coisa que separa uma conversa da
     seguinte — então já nasce com hora, não só data. Com data apenas, 27
     sessões do mesmo dia ficavam com o texto idêntico.
     `project_name` só existe onde a lista cruza projetos (o gráfico do Codex);
     dentro da gaveta de um projeto ele não vem, e dizer "sem projeto" ali seria
     mentira — estamos dentro dele. */
  return ((t.project_name || 'sem título') + ' · ' + dataHora(t)).slice(0, corte);
}

function dataCurta(t) {
  const ts = t.ended_at || t.started_at;
  return ts ? new Date(ts * 1000).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' }) : '';
}

function dataHora(t) {
  const ts = t.ended_at || t.started_at;
  if (!ts) return 'sem data';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' })
    + ' ' + d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
}

/** Desfaz empate de rótulo numa lista já montada.
 *  O corte em N caracteres faz conversas diferentes virarem o mesmo texto —
 *  "analise os arquivos dessa pasta, p" aparecia 4× no gráfico do Codex. Só dá
 *  para saber que houve colisão olhando a lista inteira, nunca item a item. */
function desempatar(itens, rotulo = r => r.rotulo, aplicar = (r, v) => { r.rotulo = v; }) {
  const vistos = new Map();
  itens.forEach(i => vistos.set(rotulo(i), (vistos.get(rotulo(i)) || 0) + 1));
  itens.forEach(i => {
    const r = rotulo(i);
    const marca = dataHora(i.origem || i);
    // O rótulo de fallback já termina em data e hora: acrescentar de novo só
    // alongaria o texto sem separar nada.
    if (vistos.get(r) > 1 && !r.endsWith(marca)) aplicar(i, r + ' · ' + marca);
  });
  return itens;
}

/** Largura útil dentro de um .chart-card, em pixels de tela.
 *  Os geradores desenham para esta medida, então o SVG sai 1:1 e a fonte do
 *  eixo tem o mesmo tamanho que o resto da interface. */
function larguraCartao() {
  const cont = $('#content');
  if (!cont) return 720;
  const cs = getComputedStyle(cont);
  const interno = cont.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  return Math.max(360, Math.floor(interno - 36));   // 18px de padding em cada lado do cartão
}

VIEWS.consumo = async function () {
  render(`<div class="page-head"><div>
      <h1 class="page-title">Consumo</h1>
      <p class="page-sub">Quando você trabalha, quanto isso processa e o que custa</p>
    </div></div><div id="cons-body"><div class="sk-line w40"></div></div>`);

  let d;
  try { d = await api('graficos'); }
  catch (err) { $('#cons-body').innerHTML = `<div class="empty"><h3>Falhou</h3><p>${esc(err.message)}</p></div>`; return; }

  const C = window.Charts;
  const LG = larguraCartao();

  /* Os dois totais NÃO medem a mesma coisa, e somá-los inventaria uma grandeza
     que não existe. O Claude grava consumo por mensagem, então dá para separar
     o que é cobrável (entrada + saída + gravação) da releitura de cache, que
     repete o mesmo prefixo a cada turno. O Codex só expõe `tokens_used` da
     thread, que é acumulado por turno — da mesma natureza da releitura. Somados,
     o Codex parecia 2× o Claude quando a razão real vai na direção oposta. */
  const somar = (ia, campo) => d.custo.linhas
    .filter(r => r.ai === ia).reduce((a, r) => a + (r[campo] || 0), 0);
  const cobravel = somar('claude', 'tok_total') + somar('antigravity', 'tok_total');
  const relido = somar('claude', 'tok_cache_r');
  const acumulado = somar('codex', 'tok_total');

  const madrugada = d.ritmo.grade.reduce((a, l) => a + l.slice(0, 6).reduce((x, y) => x + y, 0), 0);

  /* --- Fichas de destaque --- */
  const tiles = `<div class="tiles">
    <div class="tile"><div class="rot">Cobrável &middot; Claude</div>
      <div class="val">${C.compacto(cobravel)}</div>
      <div class="sub">entrada + saída + gravação de cache${
        relido ? `<br>releitura de cache: ${C.compacto(relido)}, à parte` : ''}</div></div>
    <div class="tile"><div class="rot">Contexto acumulado &middot; Codex</div>
      <div class="val">${C.compacto(acumulado)}</div>
      <div class="sub">soma de cada turno, com releitura dentro &mdash;
        não comparável com o número ao lado</div></div>
    <div class="tile"><div class="rot">Mensagens registradas</div>
      <div class="val">${C.compacto(d.ritmo.total)}</div>
      <div class="sub">${madrugada} entre 0h e 6h (${Math.round(madrugada / Math.max(d.ritmo.total, 1) * 100)}%)</div></div>
    <div class="tile"><div class="rot">Custo estimado</div>
      <div class="val">${d.custo.sem_preco.length
        ? '<span style="color:var(--faint);font-size:17px">sem preços</span>'
        : d.custo.moeda + ' ' + d.custo.total.toFixed(2)}</div>
      <div class="sub">${d.custo.sem_preco.length
        ? 'informe os preços em Configurações' : 'estimativa pela sua tabela'}</div></div>
  </div>`;

  /* --- 1. Mapa de ritmo --- */
  const linhasRitmo = [];
  d.ritmo.grade.forEach((l, i) => {
    const soma = l.reduce((a, b) => a + b, 0);
    if (!soma) return;
    const pico = l.indexOf(Math.max(...l));
    linhasRitmo.push([C.DIAS[i], soma, String(pico).padStart(2, '0') + 'h', Math.max(...l)]);
  });
  const ritmo = C.cartao('ritmo', 'Ritmo de trabalho',
    'Cada célula é uma hora de um dia da semana, somando todas as semanas. Os tons são cortados por quintil — cada faixa da legenda cobre um quinto das horas com atividade.',
    C.mapaRitmo(d.ritmo.grade, { largura: LG, aria: 'Mensagens por dia da semana e hora' }),
    C.tabelaDe(['Dia', 'Mensagens', 'Hora de pico', 'No pico'], linhasRitmo),
    C.escalaCalor(d.ritmo.grade));

  /* --- 2. Consumo diário (exato, só Claude) --- */
  const dias = d.diario.filter(r => r.ai === 'claude');
  const pontos = dias.map(r => ({
    rotulo: r.day.slice(8) + '/' + r.day.slice(5, 7),
    valor: (r.tok_in || 0) + (r.tok_out || 0) + (r.tok_cache_w || 0),
    dica: `<b>${new Date(r.day + 'T12:00').toLocaleDateString('pt-BR', { day: '2-digit', month: 'long' })}</b>
      <span class="val">${C.compacto((r.tok_in || 0) + (r.tok_out || 0) + (r.tok_cache_w || 0))} tokens · ${r.msgs} mensagens</span>`,
  }));
  const diario = pontos.length ? C.cartao('diario', 'Consumo por dia — Claude',
    'Exato: o Claude grava o consumo de cada mensagem com hora. Uma série só, então não precisa de legenda.',
    C.barrasDia(pontos, { largura: LG, cor: 'var(--chart-1)', aria: 'Tokens processados por dia pelo Claude' }),
    C.tabelaDe(['Dia', 'Tokens', 'Mensagens'],
      dias.map(r => [r.day, C.compacto((r.tok_in || 0) + (r.tok_out || 0) + (r.tok_cache_w || 0)), r.msgs])))
    : '';

  /* --- 3. Codex: só total por thread --- */
  const threads = d.codex_threads.slice().sort((a, b) => b.tok_total - a.tok_total).slice(0, 12);
  const codex = threads.length ? C.cartao('codex', 'Consumo por conversa — Codex',
    `<strong>Gráfico separado de propósito.</strong> O Codex só informa o total acumulado de cada
     conversa, sem quebra por dia — misturar isso com as barras diárias do Claude inventaria uma
     precisão que o dado não tem. Aqui cada barra é uma conversa inteira.`,
    C.barrasHoriz(desempatar(threads.map(t => ({
      rotulo: rotuloConversa(t),
      origem: t,
      valor: t.tok_total, cor: 'var(--chart-3)',
      dica: `<b>${esc(rotuloConversa(t, 60))}</b><span class="val">${esc(t.project_name || 'sem projeto')} · ${esc(t.model || '')}<br>${C.compacto(t.tok_total)} tokens · ${esc(fmtDate(t.started_at))} → ${esc(fmtDate(t.ended_at))}</span>`,
    }))), { largura: LG, aria: 'Tokens por conversa do Codex' }),
    C.tabelaDe(['Conversa', 'Projeto', 'Modelo', 'Tokens'],
      threads.map(t => [rotuloConversa(t, 60), t.project_name || '—', t.model || '—', C.compacto(t.tok_total)])))
    : '';

  /* --- 4. Custo por modelo --- */
  /* Duas grandezas, dois cartões. A versão anterior punha GPT-5.6-sol (3,9 bi,
     contexto acumulado) na mesma barra que opus 5 (713 mi, cobrável) e
     compensava com cinco linhas de ressalva — o texto pedia desculpa pelo
     gráfico. O app já resolve isso separando em outros dois lugares (consumo
     diário do Claude × por conversa do Codex); aqui não separava. */
  const precoNota = d.custo.sem_preco.length
    ? `Nenhum preço informado ainda (${d.custo.sem_preco.length} ${d.custo.sem_preco.length === 1 ? 'modelo' : 'modelos'}).
       Preço não vem embutido de fábrica — um valor chutado viraria um número de dinheiro
       confiantemente errado. Informe os seus em <strong>Configurações</strong>; as barras
       mostram o volume de tokens enquanto isso.`
    : 'Estimativa a partir da tabela de preços que você informou, em ' + esc(d.custo.moeda) + ' por milhão de tokens.';

  // O aviso de preço vale para a tela toda, não por cartão: repeti-lo nos dois
  // dobrava um parágrafo que já é longo. Vai no primeiro que for desenhado.
  let precoJaDito = false;
  const cartaoCusto = (id, titulo, nota, linhas, comLegenda) => linhas.length
    ? C.cartao(id, titulo, (precoJaDito ? '' : ((precoJaDito = true), precoNota + ' ')) + nota,
        C.barrasHoriz(linhas.slice(0, 10).map(r => ({
          rotulo: shortModel(r.model) || '—',
          valor: r.tok_total,
          cor: COR_IA[r.ai] || 'var(--muted)',
          dica: `<b>${esc(r.model)}</b><span class="val">${C.compacto(r.tok_total)} tokens · ${r.n} sessões${
            r.valor !== null ? '<br>' + esc(d.custo.moeda) + ' ' + r.valor.toFixed(2) : '<br>sem preço informado'}</span>`,
        })), { largura: LG, rotuloW: 150, aria: 'Tokens por modelo — ' + titulo }),
        C.tabelaDe(['Modelo', 'IA', 'Sessões', 'Tokens', 'Custo'],
          linhas.map(r => [r.model, r.ai, r.n, C.compacto(r.tok_total),
            r.valor !== null ? d.custo.moeda + ' ' + r.valor.toFixed(2) : 'sem preço'])),
        // Legenda só quando há mais de uma IA no desenho: um item que não
        // aparece faz o leitor procurar uma cor que não existe ali.
        comLegenda ? `<span class="legend">${[...new Set(linhas.map(r => r.ai))]
          .map(a => `<span><i style="background:${COR_IA[a] || 'var(--muted)'}"></i>${AIS[a] ? AIS[a].label : a}</span>`).join('')}
        </span>` : '')
    : '';

  const comTokens = d.custo.linhas.filter(r => r.tok_total > 0);
  const linhasCobravel = comTokens.filter(r => r.granularidade !== 'total');
  const linhasAcumulado = comTokens.filter(r => r.granularidade === 'total');

  const custo = cartaoCusto('custo-cobravel', 'Custo por modelo — cobrável',
    'Cada barra é entrada + saída + gravação de cache, que é o que a fatura cobra.',
    linhasCobravel, new Set(linhasCobravel.map(r => r.ai)).size > 1)
    + cartaoCusto('custo-acumulado', 'Custo por modelo — contexto acumulado',
      `<strong>Eixo próprio de propósito.</strong> O Codex só publica o total da thread, que soma
       o contexto relido a cada turno — é da mesma natureza da releitura de cache, não do número
       cobrável ao lado. Compare estas barras entre si, nunca com as de cima.`,
      linhasAcumulado, new Set(linhasAcumulado.map(r => r.ai)).size > 1);

  $('#cons-body').innerHTML = tiles + ritmo + diario + codex + custo;
  C.ligarTabelas($('#cons-body'));
  C.ligarTips($('#cons-body'));
};

/* ---------- Faxina ---------- */

VIEWS.faxina = async function () {
  render(`<div class="page-head"><div>
      <h1 class="page-title">Faxina</h1>
      <p class="page-sub">Duplicatas, projetos parados e hist&oacute;rico ocupando disco</p>
    </div></div><div id="faxina-body"><div class="sk-line w40"></div></div>`);

  let data;
  try { data = await api('faxina'); }
  catch (err) { $('#faxina-body').innerHTML = `<div class="empty"><h3>Falhou</h3><p>${esc(err.message)}</p></div>`; return; }

  const exp = data.desprotegidos || [];
  const totalExp = exp.reduce((a, d) => a + d.arquivos, 0);
  const semNada = exp.filter(d => !d.snapshots).length;
  const desprotegidos = exp.length ? `
      <p class="chart-note" style="margin:0 0 12px">
        <strong>${totalExp}</strong> arquivos escritos por IA em <strong>${exp.length}</strong>
        ${exp.length === 1 ? 'projeto' : 'projetos'} sem reposit&oacute;rio git.
        ${semNada ? `<strong style="color:var(--warn)">${semNada}</strong>
          ${semNada === 1 ? 'deles n&atilde;o tem' : 'deles n&atilde;o t&ecirc;m'} nem snapshot &mdash;
          nenhuma forma de voltar atr&aacute;s.` : 'Todos t&ecirc;m ao menos um snapshot.'}
      </p>
      <div class="list">${exp.map(d => `
        <div class="row" style="align-items:flex-start">
          <div class="grow" data-id="${d.id}" style="cursor:pointer;min-width:0">
            <div class="ellip"><strong>${esc(d.name)}</strong></div>
            <div class="ellip mono" style="font-size:11.5px;color:var(--faint)">${esc(d.path)}</div>
          </div>
          <span class="tag ${d.snapshots ? '' : 'dirty'}" style="flex:0 0 auto">
            ${d.arquivos} ${d.arquivos === 1 ? 'arquivo' : 'arquivos'}</span>
          <span class="num" style="min-width:96px;text-align:right;color:var(--faint)">
            ${d.snapshots ? `${d.snapshots} snapshot${d.snapshots > 1 ? 's' : ''}` : 'sem snapshot'}</span>
          <button class="btn small primary" data-zip="${d.id}" data-so-ia="1" style="flex:0 0 auto"
            title="Salva s&oacute; os ${d.arquivos} arquivos que a IA escreveu — r&aacute;pido mesmo
                   em pasta gigante. Grava em %LOCALAPPDATA%, n&atilde;o toca no projeto.">Salvar os ${d.arquivos}</button>
          <button class="btn small" data-zip="${d.id}" style="flex:0 0 auto"
            title="Zipa a pasta inteira — limite de 3 GB">Pasta toda</button>
        </div>`).join('')}</div>`
    : '<p style="color:var(--muted);font-size:13px">Nenhum trabalho de IA fora de um reposit&oacute;rio.</p>';

  const dup = data.duplicatas.length ? `<div class="list">${data.duplicatas.map(g => `
      <div class="row" style="cursor:default;align-items:flex-start">
        <div class="grow">
          <div><strong>${esc(g.name)}</strong>
            <span style="color:var(--faint);font-size:12px"> &middot; ${g.copias.length} c&oacute;pias</span></div>
          ${g.copias.map(c => `<div style="font-family:var(--mono);font-size:11.5px;color:var(--muted);margin-top:3px">
              <a href="#" data-id="${c.id}" style="color:var(--text-2)">${esc(c.path)}</a>
              <span style="color:var(--faint)"> &mdash; ${c.session_count} sess&otilde;es, ${esc(fmtAgo(c.last_activity || c.fs_mtime))}</span>
            </div>`).join('')}
        </div>
      </div>`).join('')}</div>`
    : '<p style="color:var(--muted);font-size:13px">Nenhum nome de projeto repetido entre as pastas.</p>';

  const parados = data.parados.length ? `<div class="list">${data.parados.map(p => `
      <div class="row" data-id="${p.id}">
        <div class="grow"><div class="ellip"><strong>${esc(p.name)}</strong></div>
          <div style="font-family:var(--mono);font-size:11.5px;color:var(--faint)" class="ellip">${esc(p.path)}</div></div>
        ${p.size_bytes ? `<span class="num" style="color:var(--faint)">${fmtBytes(p.size_bytes)}</span>` : ''}
        <span class="num">${esc(fmtAgo(p.last_activity || p.fs_mtime))}</span>
      </div>`).join('')}</div>`
    : '<p style="color:var(--muted);font-size:13px">Nada parado h&aacute; mais de 90 dias.</p>';

  const faltam = data.total_projetos - data.medidos;
  const pesados = data.pesados.length ? `<div class="list">${data.pesados.map(p => `
      <div class="row" data-id="${p.id}">
        <div class="grow"><div class="ellip"><strong>${esc(p.name)}</strong></div>
          <div style="font-family:var(--mono);font-size:11.5px;color:var(--faint)" class="ellip">${esc(p.path)}</div></div>
        <span class="num" style="color:var(--faint)">${fmtNum(p.file_count)} arquivos</span>
        <span class="num" style="min-width:74px;text-align:right"><strong>${fmtBytes(p.size_bytes)}</strong></span>
      </div>`).join('')}</div>`
    : `<div class="note">Nenhum projeto foi medido ainda. A medi&ccedil;&atilde;o percorre o disco
       inteiro (cerca de 9 s num projeto de 39 mil arquivos), por isso ela n&atilde;o roda junto
       com a varredura normal.</div>`;

  $('#faxina-body').innerHTML = `
    <div class="section">
      <h2 class="section-title">Trabalho de IA sem rede de seguran&ccedil;a</h2>
      ${desprotegidos}
    </div>

    <div class="section">
      <h2 class="section-title">Projetos por peso em disco</h2>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
        <span style="font-size:12.5px;color:var(--muted)">
          ${data.medidos} de ${data.total_projetos} medidos${faltam ? ` &middot; faltam ${faltam}` : ''}
        </span>
        <button class="btn small" id="btn-medir">${faltam ? 'Medir tudo' : 'Medir de novo'}</button>
      </div>
      ${pesados}
    </div>

    <div class="section">
      <h2 class="section-title">Hist&oacute;rico das IAs no disco</h2>
      <table class="data"><thead><tr><th>IA</th><th class="num">Sess&otilde;es</th><th class="num">Espa&ccedil;o</th><th class="num">Maior sess&atilde;o</th></tr></thead>
      <tbody>${data.disco.map(r => `<tr>
        <td><span class="tag ${esc(r.ai)}">${esc(AIS[r.ai] ? AIS[r.ai].label : r.ai)}</span></td>
        <td class="num">${r.n}</td><td class="num">${fmtBytes(r.bytes)}</td>
        <td class="num">${fmtBytes(r.maior)}</td></tr>`).join('')}
      <tr><td><strong>Total</strong></td><td class="num"></td>
        <td class="num"><strong>${fmtBytes(data.disco.reduce((a, r) => a + (r.bytes || 0), 0))}</strong></td><td></td></tr>
      </tbody></table>
    </div>

    <div class="section">
      <h2 class="section-title">Sess&otilde;es gigantes</h2>
      <div class="list">${data.gigantes.map(s => `
        <div class="row" ${s.project_id ? `data-id="${s.project_id}"` : ''}>
          <span class="ai-dot ${esc(s.ai)}" style="flex:0 0 7px"></span>
          <div class="grow" title="${esc(s.native_id)}"><div class="ellip">${esc(rotuloConversa(s, 70))}</div>
            <div style="font-size:11.5px;color:var(--faint)" class="ellip">${esc(s.project_name || 'sem projeto')} &middot; ${esc(fmtDate(s.ended_at))}</div></div>
          <span class="num">${fmtBytes(s.size_bytes)}</span>
        </div>`).join('')}</div>
    </div>

    <div class="section"><h2 class="section-title">Poss&iacute;veis duplicatas</h2>${dup}</div>
    <div class="section"><h2 class="section-title">Parados h&aacute; mais de 90 dias</h2>${parados}</div>`;

  ligarAlvos('#faxina-body [data-id]', (el, ev) => {
    if (ev) ev.preventDefault();
    openProject(Number(el.dataset.id));
  }, el => 'Abrir ' + ((el.querySelector('strong') || {}).textContent || el.textContent.trim().slice(0, 40)));
  $$('#faxina-body [data-zip]').forEach(el => el.onclick = async ev => {
    ev.stopPropagation();
    // Snapshot grava em %LOCALAPPDATA%, fora do projeto -- por isso passa pelo
    // Modo Seguro. E' a unica rede disponivel sem desligar nada.
    const soIa = el.dataset.soIa === '1';
    const label = prompt('Rótulo do snapshot (opcional):', soIa ? 'arquivos da IA' : '');
    if (label === null) return;
    const rotulo = el.textContent;
    el.disabled = true; el.textContent = 'Compactando…';
    await runAction(`project/${el.dataset.zip}/snapshot`,
      soIa ? { label, apenas: 'ia' } : { label },
      o => `Snapshot criado — ${o.files} arquivos, ${fmtBytes(o.size)}` +
           (o.faltando ? ` (${o.faltando} já não existem no disco)` : ''));
    el.disabled = false; el.textContent = rotulo;
    VIEWS.faxina();
  });

  const medir = $('#btn-medir');
  if (medir) medir.onclick = async () => {
    medir.disabled = true;
    medir.textContent = 'Medindo…';
    await post('scan', { measure: true, git: false });
    toast('Medindo o disco — acompanhe na barra lateral');
    pollScan(() => VIEWS.faxina());
  };
};

/* ---------- Configuracoes ---------- */

VIEWS.config = async function () {
  const cfg = await api('settings');
  let modelos = [];
  try { modelos = (await api('graficos')).custo.linhas.filter(r => r.tok_total > 0); } catch (e) { /* sem índice ainda */ }
  render(`
    <div class="page-head"><div>
      <h1 class="page-title">Configura&ccedil;&otilde;es</h1>
      <p class="page-sub">Onde procurar projetos e o que o programa pode fazer</p>
    </div></div>

    <div class="field">
      <label>Pastas varridas (uma por linha)</label>
      <textarea id="cfg-roots" spellcheck="false">${esc((cfg.roots || []).join('\n'))}</textarea>
      <div class="hint">Cada subpasta direta destas vira um projeto. Deixe vazio para ver
        apenas os projetos em que as IAs realmente trabalharam.</div>
    </div>

    <div class="field">
      <label>Pastas ignoradas (uma por linha)</label>
      <textarea id="cfg-ignore" spellcheck="false" style="min-height:76px">${esc((cfg.ignore_names || []).join('\n'))}</textarea>
    </div>

    <div class="field">
      <label>Janela do selo &ldquo;ao vivo&rdquo; (minutos)</label>
      <input type="number" id="cfg-window" min="1" max="720" value="${Number(cfg.live_window_minutes || 30)}">
      <div class="hint">Um projeto acende quando o arquivo de sess&atilde;o da IA foi escrito dentro desse per&iacute;odo.</div>
    </div>

    <div class="field">
      <label class="switch">
        <input type="checkbox" id="cfg-safe" ${cfg.safe_mode ? 'checked' : ''}>
        <span class="track"></span>
        <span>Modo Seguro &mdash; o programa nunca escreve nos seus projetos</span>
      </label>
      <div class="hint">Ligado, as a&ccedil;&otilde;es de git e snapshot ficam vis&iacute;veis por&eacute;m bloqueadas, inclusive no servidor.
        Desligue apenas quando quiser commitar pelo pr&oacute;prio painel.</div>
    </div>

    <div class="field">
      <label class="switch">
        <input type="checkbox" id="cfg-adopt" ${cfg.adopt_discovered ? 'checked' : ''}>
        <span class="track"></span>
        <span>Adotar projetos descobertos fora das pastas configuradas</span>
      </label>
      <div class="hint">Cria uma entrada quando uma IA trabalhou num diret&oacute;rio fora da lista (marcado com <strong>*</strong>).</div>
    </div>

    <div style="display:flex;gap:8px;margin-top:22px">
      <button class="btn primary" id="cfg-save">Salvar</button>
      <button class="btn" id="cfg-rescan">Salvar e varrer tudo</button>
    </div>

    <div class="section" style="margin-top:34px">
      <h2 class="section-title">Pre&ccedil;os por milh&atilde;o de tokens</h2>
      <p style="font-size:12.5px;color:var(--muted);margin:0 0 12px;max-width:620px">
        Nenhum pre&ccedil;o vem de f&aacute;brica: um valor chutado viraria um n&uacute;mero de dinheiro
        confiantemente errado. Preencha em <strong>${esc(cfg.currency || 'USD')} por milh&atilde;o</strong> e o
        Consumo passa a estimar. Deixe zerado o que n&atilde;o quiser contar.
        <br>O Codex n&atilde;o separa entrada de sa&iacute;da &mdash; para os modelos dele, use a coluna
        <em>Sa&iacute;da</em> como pre&ccedil;o &uacute;nico do total.
      </p>
      ${modelos.length ? `<table class="data"><thead><tr>
        <th>Modelo</th><th>IA</th><th class="num">Entrada</th><th class="num">Sa&iacute;da</th>
        <th class="num">Cache grav.</th><th class="num">Cache lido</th></tr></thead><tbody>
        ${modelos.map(m => {
          const pr = (cfg.prices || {})[m.model] || {};
          const campo = (k, v) => `<input type="number" step="0.01" min="0" data-preco="${esc(m.model)}"
            data-campo="${k}" value="${Number(v || 0)}"
            style="width:78px;text-align:right;background:var(--surface);border:1px solid var(--line-soft);
                   border-radius:var(--r-sm);color:var(--text);padding:4px 7px;font:inherit;font-size:12.5px">`;
          return `<tr><td class="mono" style="font-size:12px">${esc(m.model)}</td>
            <td><span class="tag ${esc(m.ai)}">${esc(AIS[m.ai] ? AIS[m.ai].label : m.ai)}</span></td>
            <td class="num">${m.granularidade === 'total' ? '<span style="color:var(--faint)">n/d</span>' : campo('in', pr.in)}</td>
            <td class="num">${campo('out', pr.out)}</td>
            <td class="num">${m.granularidade === 'total' ? '<span style="color:var(--faint)">n/d</span>' : campo('cache_w', pr.cache_w)}</td>
            <td class="num">${m.granularidade === 'total' ? '<span style="color:var(--faint)">n/d</span>' : campo('cache_r', pr.cache_r)}</td>
          </tr>`;
        }).join('')}
      </tbody></table>` : '<p style="font-size:12.5px;color:var(--faint)">Nenhum modelo no índice ainda.</p>'}
      <button class="btn primary small" id="cfg-precos" style="margin-top:12px">Salvar pre&ccedil;os</button>
    </div>

    <div class="section" style="margin-top:34px">
      <h2 class="section-title">&Iacute;ndice</h2>
      <dl class="kv">
        <dt>Projetos</dt><dd>${state.boot ? state.boot.totals.projects : '—'}</dd>
        <dt>Sess&otilde;es</dt><dd>${state.boot ? state.boot.totals.sessions : '—'}</dd>
        <dt>Mensagens</dt><dd>${state.boot ? state.boot.totals.events : '—'}</dd>
        <dt>Artefatos</dt><dd>${state.boot ? state.boot.totals.artifacts : '—'}</dd>
        <dt>&Uacute;ltima varredura</dt><dd>${state.boot ? esc(fmtDate(state.boot.last_scan, true)) : '—'}</dd>
      </dl>
    </div>
  `);

  const collect = () => ({
    roots: $('#cfg-roots').value.split('\n').map(s => s.trim()).filter(Boolean),
    ignore_names: $('#cfg-ignore').value.split('\n').map(s => s.trim()).filter(Boolean),
    live_window_minutes: Number($('#cfg-window').value) || 30,
    safe_mode: $('#cfg-safe').checked,
    adopt_discovered: $('#cfg-adopt').checked,
  });

  $('#cfg-save').onclick = async () => {
    await post('settings', collect());
    await refreshBoot();
    toast('Configurações salvas', 'ok');
  };
  const botaoPrecos = $('#cfg-precos');
  if (botaoPrecos) botaoPrecos.onclick = async () => {
    const precos = {};
    $$('[data-preco]').forEach(inp => {
      const v = parseFloat(inp.value) || 0;
      if (!v) return;
      (precos[inp.dataset.preco] = precos[inp.dataset.preco] || {})[inp.dataset.campo] = v;
    });
    await post('settings', { prices: precos });
    await refreshBoot();
    toast('Preços salvos — veja a estimativa em Consumo', 'ok');
  };
  $('#cfg-rescan').onclick = async () => {
    await post('settings', collect());
    await post('scan', {});
    toast('Varredura iniciada', 'ok');
    pollScan();
  };
};

/* ---------- Gaveta de detalhe do projeto ---------- */

async function openProject(id, aba) {
  if (!id) return;
  // Guardado antes de qualquer redesenho: depois dele o elemento já não existe.
  if (document.activeElement && document.activeElement !== document.body) {
    state.focoAnterior = document.activeElement;
  }
  const drawer = $('#drawer'), backdrop = $('#drawer-backdrop');
  drawer.hidden = false; backdrop.hidden = false;
  drawer.innerHTML = `<div class="drawer-head"><div class="drawer-title">
      <h2>Carregando&hellip;</h2><button class="drawer-close" id="dclose">&times;</button></div></div>`;
  $('#dclose').onclick = closeDrawer;

  let p;
  try { p = await api('project/' + id); }
  catch (err) { toast('Não deu para abrir o projeto: ' + err.message, 'err'); closeDrawer(); return; }

  state.openProject = p;
  // Começa com o que está pendente marcado: é o motivo de alguém abrir a aba.
  state.arqSel = new Set(
    ((p.touched || {}).itens || []).filter(a => a.pendente).map(caminhoGit));
  state.drawerTab = ['visao', 'sessoes', 'arquivos', 'git', 'contextos', 'imagens', 'snapshots', 'custo']
    .includes(aba) ? aba : 'visao';
  irPara('#/projeto/' + p.id + '/' + state.drawerTab, true);
  drawDrawer();
}

const OPEN_LABELS = {
  explorer: 'Explorador', vscode: 'VS Code', antigravity: 'Antigravity',
  claude: 'Claude Code', codex: 'Codex', terminal: 'Terminal',
};

/* Seis botões de "abrir em" ocupavam uma faixa inteira do cabeçalho da gaveta,
   que já come 22% da altura da janela. São ações secundárias — quem abre a
   gaveta vem ler as abas —, então viraram um <select> de uma linha só, na mesma
   linha do título. O Snapshot fica ao lado, porque é ação de uma clique. */
function actionBar(p) {
  const targets = p.targets || {};
  const disponiveis = Object.keys(OPEN_LABELS).filter(t => targets[t]);
  if (!disponiveis.length && !p.on_disk) return '';
  return `
    <select class="btn small" id="abrir-em" aria-label="Abrir este projeto em outro programa"
            style="padding:0 6px;border:none">
      <option value="">Abrir em…</option>
      ${disponiveis.map(t => `<option value="${t}">${OPEN_LABELS[t]}</option>`).join('')}
    </select>
    <button class="btn small" data-snapshot="1"
      title="Cria um .zip carimbado em %LOCALAPPDATA%\\ProjectCommit\\snapshots">Snapshot .zip</button>`;
}

async function runAction(path, body, okMessage) {
  try {
    const out = await post(path, body);
    toast(typeof okMessage === 'function' ? okMessage(out) : okMessage, 'ok');
    return out;
  } catch (err) {
    toast(err.message, 'err');
    return null;
  }
}

function wireActions(p) {
  const abrir = $('#abrir-em');
  if (abrir) abrir.onchange = ev => {
    const alvo = ev.target.value;
    ev.target.value = '';   // volta ao rótulo, para poder repetir a mesma escolha
    if (alvo) runAction(`project/${p.id}/open`, { target: alvo },
      `Abrindo em ${OPEN_LABELS[alvo]}…`);
  };

  $$('#drawer [data-restore]').forEach(el => el.onclick = async () => {
    const safe = state.boot && state.boot.settings.safe_mode;
    if (safe) { toast('Restaurar escreve no projeto — desligue o Modo Seguro em Configurações', 'err'); return; }
    if (!confirm(
      'Restaurar vai SOBRESCREVER os arquivos atuais de "' + p.name + '" com os do snapshot.\n\n' +
      'Antes disso será criado um snapshot do estado de agora, então dá para voltar.\n\nContinuar?')) return;
    el.disabled = true; el.textContent = 'Restaurando…';
    const out = await runAction(`project/${p.id}/restore`, { zip: el.dataset.restore },
      o => `${o.files} arquivos restaurados`);
    el.disabled = false; el.textContent = 'Restaurar';
    if (out) openProject(p.id);
  });

  $$('#drawer [data-snapshot]').forEach(el => el.onclick = async () => {
    const label = prompt('Rótulo do snapshot (opcional):', '');
    if (label === null) return;
    el.disabled = true;
    await runAction(`project/${p.id}/snapshot`, { label },
      out => `Snapshot criado: ${out.files} arquivos, ${out.size_human}`);
    el.disabled = false;
  });

  $$('#drawer [data-action]').forEach(el => el.onclick = async () => {
    const acao = el.dataset.action;
    const sugestao = p.sugestao || 'Atualiza o projeto';

    if (acao === 'git-init') {
      // O preflight percorre o disco inteiro — 9 s num projeto de 39 mil
      // arquivos. Por isso ele roda só aqui, no único momento em que a resposta
      // muda a decisão, e não ao abrir a gaveta.
      const rotulo = el.textContent;
      el.disabled = true;
      el.textContent = 'Medindo o projeto…';
      let pre = null;
      try { pre = await api(`project/${p.id}/preflight`); } catch (e) { /* segue sem o aviso */ }
      el.textContent = rotulo;
      el.disabled = false;
      if (pre && pre.pesado && !confirm(
        `Este projeto tem ${pre.files}${pre.truncated ? '+' : ''} arquivos (${fmtBytes(pre.size)}).\n\n` +
        'Será criado um .gitignore padrão antes do primeiro commit, mas confira se ' +
        'não há material pesado que você não quer versionar.\n\nContinuar?')) return;
      el.disabled = true;
      const out = await runAction(`project/${p.id}/git-init`, {},
        o => o.committed ? `Repositório criado e commitado (${o.sha})` : 'Repositório criado');
      el.disabled = false;
      if (out) openProject(p.id);
      return;
    }

    if (acao === 'git-commit') {
      const msg = prompt('Mensagem do commit:', sugestao);
      if (msg === null) return;
      el.disabled = true;
      const out = await runAction(`project/${p.id}/commit`, { message: msg },
        o => `Commit ${o.sha} — ${o.files} arquivos`);
      el.disabled = false;
      if (out) openProject(p.id);
    }
  });
}

function closeDrawer() {
  $('#drawer').hidden = true;
  $('#drawer-backdrop').hidden = true;
  // Sem limpar a URL, um F5 depois de fechar reabria a gaveta que você acabou
  // de fechar. Volta para a tela de trás, que é de onde a gaveta foi aberta.
  if (location.hash.startsWith('#/projeto/')) {
    irPara('#/' + (TELAS.includes(state.view) ? state.view : 'radar'));
  }
  state.openProject = null;
  state.transcript = null;
  // Devolve o foco a quem abriu. Sem isto, fechar com Escape jogava o foco para
  // o <body> e a próxima tecla Tab recomeçava do topo da página.
  const volta = state.focoAnterior;
  state.focoAnterior = null;
  if (volta && document.contains(volta)) volta.focus();
}

function drawDrawer() {
  const p = state.openProject;
  if (!p) return;
  const tabs = [
    ['visao',     'Visão geral',   null],
    ['sessoes',   'Sessões',  p.sessions.length],
    ['arquivos',  'Arquivos',      (p.touched || {}).total || null],
    ['git',       'Git',           p.git && p.git.has_git ? (p.commits || []).length : null],
    ['contextos', 'Contextos',     p.context_files.length + p.artifacts.filter(a => a.kind === 'context').length],
    ['imagens',   'Imagens',       imagensDoProjeto(p).length],
    ['snapshots', 'Snapshots',     (p.snapshots || []).length],
    ['custo',     'Custo',         null],
  ];

  $('#drawer').innerHTML = `
    <div class="drawer-head">
      <div class="drawer-title">
        <h2 title="${esc(p.path)}">${esc(p.name)}</h2>
        ${actionBar(p)}
        <button class="drawer-close" id="dclose" title="Fechar">&times;</button>
      </div>
      <div class="drawer-path" title="${esc(p.path)}">${esc(p.path)}</div>
      <div class="tabs">${tabs.map(([key, label, count]) =>
        `<button class="tab ${state.drawerTab === key ? 'on' : ''}" data-tab="${key}">${label}${
          count !== null && count !== undefined ? `<span class="count">${count}</span>` : ''}</button>`).join('')}
      </div>
    </div>
    <div class="drawer-body" id="drawer-body">${drawerTabHtml(p)}</div>`;

  $('#dclose').onclick = closeDrawer;
  // A aba vai para a URL: `F5` devolve onde você estava, `Voltar` desfaz um
  // passo de verdade e dá para mandar o link de uma aba específica. O redesenho
  // é feito aqui mesmo; navigate() detecta que o projeto já está aberto e sai.
  $$('#drawer .tab').forEach(el => el.onclick = () => {
    state.drawerTab = el.dataset.tab;
    irPara('#/projeto/' + p.id + '/' + el.dataset.tab);
    drawDrawer();
  });
  $$('#drawer [data-img]').forEach(el => el.onclick = () => {
    state.filtroImagem = el.dataset.img;
    drawDrawer();
  });
  $$('#drawer [data-arq]').forEach(el => el.onclick = () => {
    state.filtroArquivo = el.dataset.arq;
    drawDrawer();
  });
  $$('#drawer [data-ordem]').forEach(el => el.onclick = () => {
    state.ordemArquivo = el.dataset.ordem;
    drawDrawer();
  });
  $$('#drawer [data-fam]').forEach(el => el.onclick = () => {
    state.filtroFamilia = el.dataset.fam;
    drawDrawer();
  });
  // Os blocos da Visão geral são RESUMOS: cada um leva para a aba que tem o
  // assunto inteiro. Por `ligarAlvo`, porque a ação é abrir -- vale o mesmo
  // teclado que os cards do Radar.
  ligarAlvos('#drawer [data-ir]', el => {
    state.drawerTab = el.dataset.ir;
    irPara('#/projeto/' + p.id + '/' + el.dataset.ir);
    drawDrawer();
  }, el => {
    // O rotulo do leitor de tela vem do proprio botao da aba, nao da chave do
    // objeto: "Abrir a aba arquivos" e "Abrir a aba Arquivos 629" nao sao a
    // mesma frase, e quem ouve precisa da segunda.
    const aba = $(`#drawer .tab[data-tab="${el.dataset.ir}"]`);
    if (!aba) return 'Abrir a aba ' + el.dataset.ir;
    // `textContent` cola o contador no nome ("Arquivos56"). O nome esta' no
    // primeiro no' de texto; o contador vira uma frase separada por virgula.
    const nome = (aba.firstChild ? aba.firstChild.textContent : '').trim();
    const conta = aba.querySelector('.count');
    return 'Abrir a aba ' + nome + (conta ? ', ' + conta.textContent.trim() : '');
  });
  wireVisao(p);
  $$('#drawer [data-goto-arq]').forEach(el => el.onclick = ev => {
    ev.preventDefault();
    state.drawerTab = 'arquivos';
    state.filtroArquivo = 'pendentes';
    drawDrawer();
  });
  wireSelecaoArquivos();
  wireArtifactLinks();
  wireSessionLinks();
  wireActions(p);
  if (window.Charts) window.Charts.ligarTips($('#drawer'));
}

function drawerTabHtml(p) {
  switch (state.drawerTab) {
    case 'visao':     return tabVisao(p);
    case 'arquivos':  return tabArquivos(p);
    case 'git':       return tabGit(p);
    case 'contextos': return tabContextos(p);
    case 'imagens':   return tabImagens(p);
    case 'snapshots': return tabSnapshots(p);
    case 'custo':     return tabCusto(p);
    default:          return tabSessoes(p);
  }
}

function tabSessoes(p) {
  if (!p.sessions.length) {
    return `<div class="empty"><h3>Nenhuma IA trabalhou aqui</h3>
      <p>Este projeto est&aacute; no disco, mas nenhuma sess&atilde;o do Claude, Codex ou Antigravity aponta para ele.</p></div>`;
  }
  // Rótulos resolvidos de uma vez para o desempate enxergar a lista inteira.
  const sessoes = desempatar(
    p.sessions.map(s => Object.assign({}, s, { rotulo: rotuloConversa(s, 70) })));
  return linhaDeVidaHtml(p) + `<div class="list">${sessoes.map(s => `
    <div class="row" data-sid="${esc(s.id)}" title="Abrir a conversa" style="align-items:flex-start">
      <span class="ai-dot ${esc(s.ai)}" style="flex:0 0 7px;margin-top:6px"></span>
      <div class="grow" title="${esc(s.native_id)}">
        <div class="ellip"><strong>${esc(s.rotulo)}</strong></div>
        <div style="font-size:12px;color:var(--muted);margin-top:2px">
          ${esc(AIS[s.ai] ? AIS[s.ai].label : s.ai)}
          ${s.model ? ' &middot; ' + esc(shortModel(s.model)) : ''}
          ${s.cli_version ? ' &middot; v' + esc(s.cli_version) : ''}
          ${s.git_branch ? ' &middot; ' + esc(s.git_branch) : ''}
          ${s.sub_count ? ' &middot; ' + s.sub_count + ' subagentes' : ''}
        </div>
        ${tituloUsavel(s.first_prompt) && !s.rotulo.startsWith(tituloUsavel(s.first_prompt).slice(0, 20))
          ? `<div style="font-size:12.5px;color:var(--faint);margin-top:5px" class="ellip">${esc(s.first_prompt)}</div>` : ''}
      </div>
      <div style="text-align:right;flex:0 0 auto">
        <div class="num">${esc(fmtDate(s.ended_at))}</div>
        <div style="font-size:11.5px;color:var(--faint)">${fmtNum(s.tok_total)} tokens</div>
      </div>
    </div>`).join('')}</div>`;
}

const COR_IA = { claude: 'var(--chart-1)', antigravity: 'var(--chart-2)', codex: 'var(--chart-3)' };

function linhaDeVidaHtml(p) {
  const sessoes = p.sessions
    .filter(s => s.started_at || s.ended_at)
    .map(s => ({
      ai: AIS[s.ai] ? AIS[s.ai].label : s.ai,
      inicio: s.started_at || s.ended_at,
      fim: s.ended_at || s.started_at,
      cor: COR_IA[s.ai] || 'var(--muted)',
      dica: `<b>${esc(rotuloConversa(s, 60))}</b><span class="val">${esc(shortModel(s.model) || s.ai)}
             · ${fmtNum(s.tok_total)} tokens<br>${esc(fmtDate(s.started_at, true))}</span>`,
    }));
  if (!sessoes.length) return '';

  const commits = (p.commits || []).filter(c => c.ts).map(c => ({
    ts: c.ts,
    dica: `<b>${esc(c.short)}</b><span class="val">${esc(c.message)}<br>${esc(fmtDate(c.ts, true))}</span>`,
  }));

  const corpo = $('#drawer-body');
  const largura = corpo ? Math.max(360, corpo.clientWidth - 80) : 620;
  const svg = window.Charts.linhaDeVida(sessoes, commits,
    { largura, aria: 'Sessões de IA e commits ao longo do tempo' });
  if (!svg) return '';

  const alerta = p.git && p.git.has_git && p.git.last_ts
    && Math.max(...sessoes.map(s => s.fim)) > p.git.last_ts;

  const html = `<div class="chart-card" style="margin-bottom:18px">
      <div class="chart-head"><h3 class="chart-title">Linha de vida</h3>
        <div class="spacer"></div>
        <span class="legend">
          <span style="color:var(--faint)">menos</span>
          ${[1, 2, 3, 4, 5].map(g => `<i style="background:var(--heat-${g})"></i>`).join('')}
          <span style="color:var(--faint)">mais</span>
          ${commits.length ? '<span style="margin-left:8px"><i style="background:var(--text-2);width:2px;height:11px"></i>commit</span>' : ''}
        </span>
      </div>
      <p class="chart-note">Cada coluna é um período, e o tom diz quanto trabalho houve nele.
        Uma faixa por IA.
        ${alerta ? `<span style="color:var(--warn)">O trabalho passa do último commit —
          houve coisa de IA que nunca foi salva.</span>` : ''}</p>
      ${svg}
    </div>`;
  return html;
}

/* Famílias de arquivo: seis glifos monocromáticos, não quarenta cores. A cor
   neste app é identidade de IA — pintar cada extensão destruiria essa leitura.
   O que distingue aqui é a FORMA. */
const FAMILIAS = {
  codigo:    ['código',     'M9 6 4 11l5 5M15 6l5 5-5 5'],
  estilo:    ['estilo',     'M4 6h16M4 11h10M4 16h13'],
  documento: ['documento',  'M6 3h8l4 4v14H6zM14 3v4h4'],
  dado:      ['dado',       'M5 7c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3zM5 7v10c0 1.7 3.1 3 7 3s7-1.3 7-3V7'],
  config:    ['configuração', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1'],
  midia:     ['mídia',      'M4 5h16v14H4zM4 10h16M9 5v5M15 5v5'],
  outro:     ['outro',      'M6 3h8l4 4v14H6z'],
};

function iconeFamilia(fam, tam = 13) {
  const [rotulo, d] = FAMILIAS[fam] || FAMILIAS.outro;
  return `<svg viewBox="0 0 24 24" width="${tam}" height="${tam}" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
    aria-label="${esc(rotulo)}" role="img" style="flex:0 0 auto;opacity:.75"><path d="${d}"/></svg>`;
}

/* Um arquivo com pendência no git não é tudo a mesma coisa: "??" nunca entrou
   em commit nenhum, " M" já está versionado e só tem alteração por salvar. */
const PENDENCIA = {
  '??': ['nunca commitado', 'novo'],
  'A':  ['no stage',        'stage'],
  'M':  ['alterado',        'alt'],
  'MM': ['alterado',        'alt'],
  'AM': ['alterado',        'alt'],
  'D':  ['apagado',         'del'],
  'R':  ['renomeado',       'alt'],
};

function rotuloPendencia(status) {
  return PENDENCIA[status] || PENDENCIA[(status || '').trim()] || ['pendente', 'alt'];
}

/* O git entende barra normal no pathspec; a barra invertida do Windows ele trata
   como escape. Tudo que vai para `git add` passa por aqui. */
function caminhoGit(a) {
  return (a.rel || '').replace(/\\/g, '/');
}

/* ---------- Visão geral ---------- */

function tabVisao(p) {
  const t = p.touched || {}, m = p.metas || {}, s = p.sinais || {}, pa = p.pastas || {};
  const g = p.git || {};
  const ias = [...new Set((p.sessions || []).map(x => x.ai).filter(Boolean))];

  // Quatro zeros em fila nao informam nada -- e' o primeiro lugar onde o olho
  // cai. Um terco dos projetos nunca recebeu IA; para eles a frase vale mais
  // que a grade, e o que importa (as metas) ganha o topo.
  if (!p.sessions.length && !(t.total || 0)) {
    return `
      <p class="chart-note" style="margin:0 0 16px">Nenhuma sessão de IA registrada
        neste projeto — nada foi editado por Claude, Codex ou Antigravity aqui.</p>
      ${blocoMetas(m)}
      <div class="visao-grade">${blocoGit(p, g, t)}${blocoDocs(p)}</div>
      ${blocoDossie()}`;
  }

  return `
    <div class="visao-topo">
      <div>
        <div class="visao-num">${p.sessions.length}</div>
        <div class="visao-rot">${p.sessions.length === 1 ? 'sessão' : 'sessões'} de IA</div>
      </div>
      <div title="Arquivos que Claude, Codex ou Antigravity escreveram dentro desta pasta">
        <div class="visao-num">${t.total || 0}</div>
        <div class="visao-rot">arquivos${t.bytes ? ' · ' + fmtBytes(t.bytes) : ' editados'}</div>
      </div>
      <div title="Dos arquivos que a IA editou, quantos ainda têm alteração fora do git.
O bloco Versionamento mostra o total do git, que inclui o que você mexeu à mão.">
        <div class="visao-num ${t.pendentes ? 'alerta' : ''}">${t.pendentes || 0}</div>
        <div class="visao-rot">desses, sem commit</div>
      </div>
      <div>
        <div class="visao-num">${ias.length ? aiDots(ias) : '—'}</div>
        <div class="visao-rot">${ias.map(a => AIS[a] ? AIS[a].label : a).join(' · ') || 'nenhuma IA'}</div>
      </div>
    </div>

    ${blocoMetas(m)}

    <div class="visao-grade">
      ${blocoGit(p, g, t)}
      ${blocoPastas(pa)}
      ${blocoSinais(s)}
      ${blocoDocs(p)}
    </div>

    ${blocoDossie()}`;
}

function blocoDossie() {
  return `<div class="section" style="margin-top:6px">
      <h3 class="section-title">Levar este projeto para outra IA</h3>
      <p class="chart-note" style="margin:0 0 12px">
        Gera um resumo em Markdown com tudo desta tela — pastas, git, metas, arquivos e
        as últimas conversas — para colar numa conversa nova. O conteúdo dos documentos
        não vai junto: o dossiê dá os caminhos, e a IA abre o que precisar.</p>
      <button class="btn primary small" id="btn-dossie">Gerar dossiê</button>
      <button class="btn small" id="btn-dossie-copiar" hidden>Copiar</button>
      <div id="dossie-saida" style="margin-top:10px"></div>
    </div>`;
}

function blocoMetas(m) {
  if (!m.tem) {
    return `<div class="section">
      <h3 class="section-title">Metas</h3>
      <p class="chart-note" style="margin:0 0 10px">
        Este projeto <strong>não declara metas</strong>, então não há o que medir — e o painel
        não inventa barra de progresso. Crie um <code>ROADMAP.md</code> (ou <code>TODO.md</code>,
        <code>PLANO.md</code>) com caixas e ele passa a ler daqui sozinho.</p>
      <pre class="exemplo-metas">${esc(m.exemplo || '')}</pre>
      <button class="btn small" id="btn-copiar-exemplo">Copiar o modelo</button>
      <span style="font-size:11.5px;color:var(--faint);margin-left:8px">
        o <code>[~]</code> vale meio ponto, para o que está pela metade</span>
    </div>`;
  }
  const abertas = (m.grupos || []).flatMap(g =>
    g.itens.filter(i => i.estado !== 'feita').map(i => ({ grupo: g.titulo, ...i })));
  return `<div class="section">
    <div class="chart-head" style="margin-bottom:8px">
      <h3 class="section-title" style="margin:0">Metas</h3>
      <div class="spacer"></div>
      <span style="font-size:12px;color:var(--muted)">
        ${m.feitas} feitas · ${m.parciais} parciais · ${m.abertas} abertas
        <span style="color:var(--faint)"> — ${esc((m.arquivos || []).join(', '))}</span></span>
    </div>
    <div class="meta-linha">
      <span class="meta-pct">${m.percentual}<small>%</small></span>
      <div class="meta-barra" title="${m.feitas} feitas + ${m.parciais} parciais valendo meio ponto, de ${m.total}">
        <div class="meta-cheia" style="width:${(m.feitas / m.total * 100).toFixed(1)}%"></div>
        <div class="meta-meia" style="width:${(m.parciais * 50 / m.total).toFixed(1)}%"></div>
      </div>
    </div>
    ${abertas.length ? `<div class="list" style="margin-top:12px">${abertas.slice(0, 14).map(i => `
      <div class="row" style="cursor:default;padding:5px 12px;align-items:flex-start">
        <span class="meta-caixa ${i.estado}" title="${i.estado === 'parcial' ? 'começada' : 'não começada'}"></span>
        <div class="grow">
          <div style="font-size:12.5px">${esc(i.texto)}</div>
          <div style="font-size:11px;color:var(--faint);margin-top:2px">${esc(i.grupo)}</div>
        </div>
      </div>`).join('')}</div>
      ${abertas.length > 14 ? `<div style="font-size:11.5px;color:var(--faint);margin-top:8px">
        e mais ${abertas.length - 14}</div>` : ''}`
    : '<p class="chart-note" style="margin-top:10px">Todas as metas declaradas estão concluídas.</p>'}
  </div>`;
}

function blocoGit(p, g, t) {
  const corpo = !g.has_git
    ? `<div class="visao-vazio">Sem repositório${t.total
        ? ` — e a IA já escreveu em <strong>${t.total}</strong> arquivos aqui` : ''}.</div>`
    : `<dl class="kv compacta">
        <dt>Branch</dt><dd class="mono">${esc(g.branch || '—')}</dd>
        <dt>Commits</dt><dd>${g.commit_count !== null ? g.commit_count : '—'}</dd>
        <dt title="Tudo que o git vê alterado, inclusive o que não veio de IA">Sujo no git</dt>
        <dd>${g.dirty_count
          ? `<span style="color:var(--warn)">${g.dirty_count} arquivos</span>` : 'nada'}</dd>
        <dt>Último</dt><dd>${esc(fmtDate(g.last_ts))}</dd>
      </dl>`;
  return `<div class="visao-bloco" data-ir="git">
    <h4>Versionamento</h4>${corpo}</div>`;
}

function blocoPastas(pa) {
  const dentro = pa.dentro || [], fora = pa.fora || [];
  if (!dentro.length && !fora.length) {
    return `<div class="visao-bloco"><h4>Pastas ligadas</h4>
      <div class="visao-vazio">Nenhuma escrita de IA registrada aqui.</div></div>`;
  }
  return `<div class="visao-bloco" data-ir="arquivos">
    <h4>Pastas ligadas</h4>
    <div class="list compacta">${dentro.slice(0, 5).map(b => `
      <div class="row" style="cursor:default;padding:3px 0">
        <span class="grow ellip mono" style="font-size:12px">${esc(b.nome)}</span>
        <span class="num" style="font-size:11.5px">${b.arquivos} arq · ${b.escritas}×</span>
      </div>`).join('')}</div>
    ${fora.length ? `<div style="margin-top:8px;font-size:11.5px;color:var(--faint)"
        title="${esc(fora.slice(0, 6).map(b => b.nome).join('\n'))}">
      Também escreveu em <strong>${fora.length}</strong> ${fora.length === 1 ? 'pasta' : 'pastas'}
      fora deste projeto.</div>` : ''}
  </div>`;
}

function blocoSinais(s) {
  if (!s.tem) {
    return `<div class="visao-bloco"><h4>Marcadores no código</h4>
      <div class="visao-vazio">Nenhum <code>TODO:</code> ou <code>FIXME:</code> nos arquivos
        que a IA edita.</div></div>`;
  }
  const n = s.por_nivel || {};
  return `<div class="visao-bloco">
    <h4>Marcadores no código</h4>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      ${['alto', 'medio', 'baixo'].filter(k => n[k]).map(k =>
        `<span class="tag ${k === 'alto' ? 'dirty' : ''}">${n[k]} ${k}</span>`).join('')}
    </div>
    <div class="list compacta">${(s.itens || []).slice(0, 4).map(i => `
      <div class="row" style="cursor:default;padding:3px 0;align-items:flex-start">
        <span class="tag mono" style="flex:0 0 auto">${esc(i.marca)}</span>
        <span class="grow ellip" style="font-size:11.5px" title="${esc(i.rel + ':' + i.linha)}">${esc(i.texto || i.rel)}</span>
      </div>`).join('')}</div>
    <div style="margin-top:6px;font-size:11px;color:var(--faint)">
      É o que está escrito em comentário, não uma contagem de bugs.</div>
  </div>`;
}

function blocoDocs(p) {
  const docs = p.context_files || [];
  if (!docs.length) {
    return `<div class="visao-bloco"><h4>Documentos</h4>
      <div class="visao-vazio">Sem <code>CLAUDE.md</code>, <code>AGENTS.md</code> ou
        <code>README.md</code> na raiz.</div></div>`;
  }
  return `<div class="visao-bloco" data-ir="contextos">
    <h4>Documentos</h4>
    <div class="list compacta">${docs.slice(0, 5).map(d => `
      <div class="row" style="cursor:default;padding:3px 0" title="${esc(d.path)}">
        <span class="grow ellip mono" style="font-size:12px">${esc(d.name)}</span>
        <span class="num" style="font-size:11.5px">${fmtBytes(d.size || 0)}</span>
      </div>`).join('')}</div>
  </div>`;
}

function tabArquivos(p) {
  const t = p.touched || { itens: [], total: 0, pendentes: 0, novos: 0, cortado: 0 };
  if (!t.total) {
    return `<div class="empty">
      <h3>Nenhum arquivo registrado</h3>
      <p>As IAs registram cada escrita na pr&oacute;pria conversa &mdash; <em>Edit</em> e <em>Write</em>
         no Claude, <em>apply_patch</em> no Codex. Aqui ainda n&atilde;o apareceu nenhuma;
         atualize o &iacute;ndice se este projeto for recente.</p></div>`;
  }
  const filtro = state.filtroArquivo || 'todos';
  const fam = state.filtroFamilia || 'todas';
  const ordem = state.ordemArquivo || 'editados';

  /* A ordenação é no cliente porque o servidor já manda a lista inteira (até
     1500; o maior projeto medido tem 629). Ordenar no servidor exigiria uma ida
     à rede por clique, para reordenar algo que já está na memória. */
  const ORDENS = {
    editados: (a, b) => (b.edits || 0) - (a.edits || 0),
    pesados:  (a, b) => (b.bytes || 0) - (a.bytes || 0),
    recentes: (a, b) => (b.last_ts || 0) - (a.last_ts || 0),
    sessoes:  (a, b) => (b.sessoes || 0) - (a.sessoes || 0) || (b.edits || 0) - (a.edits || 0),
  };
  const lista = t.itens
    .filter(a => filtro !== 'pendentes' || a.pendente)
    .filter(a => fam === 'todas' || a.familia === fam)
    .slice()
    .sort(ORDENS[ordem] || ORDENS.editados);

  const resumo = p.git && p.git.has_git
    ? (t.pendentes
        ? `A IA editou <strong>${t.total}</strong> arquivos aqui.
           <strong style="color:var(--warn)">${t.pendentes}</strong> ainda t&ecirc;m altera&ccedil;&atilde;o
           fora do git${t.novos ? ` &mdash; ${t.novos} nunca commitado${t.novos > 1 ? 's' : ''}` : ''}.`
        : `A IA editou <strong>${t.total}</strong> arquivos aqui, e
           <strong style="color:var(--codex)">todos</strong> j&aacute; est&atilde;o commitados.`)
    : `A IA editou <strong>${t.total}</strong> arquivos aqui. Sem git, n&atilde;o d&aacute;
       para saber o que est&aacute; salvo.`;

  const temGit = !!(p.git && p.git.has_git);
  const sel = state.arqSel || (state.arqSel = new Set());
  const seguro = state.boot && state.boot.settings.safe_mode;

  /* "Commitar tudo" faz `git add -A` e leva junto o que você mexeu à mão. Aqui
     vai só o que está marcado — o backend já aceitava a lista de arquivos desde
     sempre (actions.git_commit(files=...)); era a interface que nunca mandava. */
  const barra = temGit ? `
    <div class="filters" style="margin:0 0 14px;align-items:center">
      <button class="btn small" id="arq-todos-sel">Marcar todos</button>
      <button class="btn small" id="arq-nenhum-sel">Desmarcar</button>
      <span style="flex:1"></span>
      <span style="font-size:12px;color:var(--faint)" id="arq-conta">${sel.size} marcado${sel.size === 1 ? '' : 's'}</span>
      <button class="btn primary small" id="arq-commit" ${seguro || !sel.size ? 'disabled' : ''}
        title="${seguro ? 'Desligue o Modo Seguro em Configurações para liberar' : 'Commita apenas os arquivos marcados'}">
        Commitar marcados</button>
    </div>
    ${seguro ? `<div class="hint" style="margin:-8px 0 14px;font-size:12px;color:var(--faint)">
      Bloqueado pelo Modo Seguro.</div>` : ''}` : '';

  return `
    <p class="chart-note" style="margin:0 0 14px">${resumo}${t.cortado
      ? ` <span style="color:var(--faint)">Mostrando os ${t.itens.length} mais editados.</span>` : ''}</p>
    <div class="filters" style="margin-bottom:8px">
      <button class="chip ${filtro === 'todos' ? 'on' : ''}" data-arq="todos">Todos
        <span style="color:var(--faint)">${t.total}</span></button>
      <button class="chip ${filtro === 'pendentes' ? 'on' : ''}" data-arq="pendentes">Sem commit
        <span style="color:var(--faint)">${t.pendentes}</span></button>
      <span style="flex:1"></span>
      ${[['editados', 'Mais editados'], ['pesados', 'Mais pesados'],
         ['recentes', 'Recentes'], ['sessoes', 'Mais sessões']].map(([k, r]) =>
        `<button class="chip ${ordem === k ? 'on' : ''}" data-ordem="${k}">${r}</button>`).join('')}
    </div>
    <div class="filters" style="margin-bottom:14px">
      <button class="chip ${fam === 'todas' ? 'on' : ''}" data-fam="todas">Todos os tipos</button>
      ${Object.keys(FAMILIAS).filter(k => (t.familias || {})[k]).map(k =>
        `<button class="chip ${fam === k ? 'on' : ''}" data-fam="${k}">${iconeFamilia(k, 12)}
           ${FAMILIAS[k][0]} <span style="color:var(--faint)">${t.familias[k]}</span></button>`).join('')}
    </div>
    ${barra}
    <div class="list">${lista.map(a => {
      const [rotulo, tom] = a.pendente ? rotuloPendencia(a.pendente) : [null, null];
      const chave = caminhoGit(a);
      return `<div class="row" style="align-items:flex-start">
        ${temGit ? `<input type="checkbox" class="arq-check" data-rel="${esc(chave)}"
           ${sel.has(chave) ? 'checked' : ''} style="flex:0 0 auto;margin-top:5px;accent-color:var(--claude)">` : ''}
        <span class="ai-dots" style="flex:0 0 auto;padding-top:5px">${
          (a.ias || []).filter(x => AIS[x]).map(x =>
            `<span class="ai-dot ${esc(x)}" title="${esc(AIS[x].label)}"></span>`).join('')}</span>
        <span style="flex:0 0 auto;padding-top:4px;color:var(--muted)">${iconeFamilia(a.familia)}</span>
        <div class="grow" style="min-width:0">
          <div class="ellip mono" style="font-size:12.5px"
               title="${esc(a.path)}">${esc(a.rel)}</div>
          <div style="font-size:11.5px;color:var(--faint);margin-top:3px">
            ${a.edits} escrita${a.edits > 1 ? 's' : ''}
            &middot; ${a.sessoes} sess${a.sessoes > 1 ? '&otilde;es' : '&atilde;o'}
            ${a.bytes != null ? ' &middot; ' + fmtBytes(a.bytes) : ''}
            ${a.last_ts ? ' &middot; ' + esc(fmtDate(a.last_ts)) : ''}
            ${a.sumiu ? ' &middot; <span style="color:var(--faint)">n&atilde;o est&aacute; mais no disco</span>' : ''}
          </div>
        </div>
        ${rotulo ? `<span class="tag ${tom === 'novo' ? 'dirty' : ''}"
                     style="flex:0 0 auto">${rotulo}</span>` : ''}
      </div>`;
    }).join('')}</div>`;
}

/** Copia texto sem depender de permissão: `127.0.0.1` é contexto seguro, mas o
 *  WebView2 pode recusar quando a janela não está em foco. Daí o plano B. */
async function copiar(texto) {
  try {
    await navigator.clipboard.writeText(texto);
    return true;
  } catch (e) {
    const t = document.createElement('textarea');
    t.value = texto;
    t.style.cssText = 'position:fixed;top:-2000px';
    document.body.appendChild(t);
    t.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    t.remove();
    return ok;
  }
}

function wireVisao(p) {
  const exemplo = $('#btn-copiar-exemplo');
  if (exemplo) exemplo.onclick = async () => {
    const ok = await copiar((p.metas || {}).exemplo || '');
    toast(ok ? 'Modelo copiado — cole num ROADMAP.md do projeto'
             : 'Não consegui copiar; o modelo está na tela', ok ? 'ok' : 'err');
  };

  const gerar = $('#btn-dossie');
  if (!gerar) return;
  gerar.onclick = async () => {
    gerar.disabled = true;
    gerar.textContent = 'Gerando…';
    const out = await runAction(`project/${p.id}/dossie`, {},
      o => `Dossiê salvo — ${fmtBytes(o.bytes)}`);
    gerar.disabled = false;
    gerar.textContent = 'Gerar dossiê';
    if (!out) return;
    state.dossie = out.texto;
    const copiarBt = $('#btn-dossie-copiar');
    if (copiarBt) {
      copiarBt.hidden = false;
      copiarBt.onclick = async () => {
        const ok = await copiar(state.dossie || '');
        toast(ok ? 'Dossiê copiado — cole numa conversa nova'
                 : 'Não consegui copiar; o arquivo está salvo', ok ? 'ok' : 'err');
      };
    }
    const saida = $('#dossie-saida');
    if (!saida) return;
    saida.innerHTML = `
      <div class="note" style="margin:0">
        Salvo em <code class="mono">${esc(out.path)}</code>
        <div style="margin-top:8px"><strong>Prévia</strong></div>
        <pre class="exemplo-metas" style="max-height:220px">${esc(out.texto.slice(0, 1400))}${
          out.texto.length > 1400 ? '\n…' : ''}</pre>
      </div>`;
    // A prévia nasce abaixo da dobra: sem isto o botão parecia não ter feito nada.
    saida.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };
}

function wireSelecaoArquivos() {
  const sel = state.arqSel;
  if (!sel) return;
  const conta = $('#arq-conta'), botao = $('#arq-commit');
  const atualiza = () => {
    if (conta) conta.textContent = `${sel.size} marcado${sel.size === 1 ? '' : 's'}`;
    const seguro = state.boot && state.boot.settings.safe_mode;
    if (botao) botao.disabled = !!seguro || !sel.size;
  };
  // Só mexe no conjunto e no contador: redesenhar a gaveta a cada clique perderia
  // a rolagem e daria a sensação de a lista "pular" enquanto você marca.
  $$('#drawer .arq-check').forEach(el => el.onchange = () => {
    if (el.checked) sel.add(el.dataset.rel); else sel.delete(el.dataset.rel);
    atualiza();
  });
  const todos = $('#arq-todos-sel'), nenhum = $('#arq-nenhum-sel');
  if (todos) todos.onclick = () => {
    $$('#drawer .arq-check').forEach(el => { el.checked = true; sel.add(el.dataset.rel); });
    atualiza();
  };
  if (nenhum) nenhum.onclick = () => {
    $$('#drawer .arq-check').forEach(el => { el.checked = false; });
    sel.clear();
    atualiza();
  };

  if (botao) botao.onclick = async () => {
    const p = state.openProject;
    const arquivos = [...sel];
    if (!p || !arquivos.length) return;
    const msg = prompt(
      `Commitar ${arquivos.length} arquivo${arquivos.length === 1 ? '' : 's'} — mensagem:`,
      p.sugestao || '');
    if (msg === null) return;
    botao.disabled = true;
    const out = await runAction(`project/${p.id}/commit`, { message: msg, files: arquivos },
      o => `Commit ${o.sha} — ${o.files} arquivos`);
    botao.disabled = false;
    if (out) openProject(p.id, 'arquivos');
  };
}

function tabGit(p) {
  if (!p.git || !p.git.has_git) {
    return `<div class="empty">
      <h3>Este projeto n&atilde;o tem git</h3>
      <p>Sem hist&oacute;rico, cada altera&ccedil;&atilde;o de uma IA sobrescreve a anterior sem rede de seguran&ccedil;a.</p>
      ${actionButton('git-init', 'Inicializar reposit&oacute;rio e fazer o primeiro commit', p.id)}
    </div>`;
  }
  const g = p.git;
  return `
    <dl class="kv" style="margin-bottom:22px">
      <dt>Branch</dt><dd class="mono">${esc(g.branch || '—')}</dd>
      <dt>Commits</dt><dd>${g.commit_count !== null ? g.commit_count : '—'}</dd>
      <dt>Sem commit</dt><dd>${g.dirty_count ? `<span style="color:var(--warn)">${g.dirty_count} arquivos</span>` : 'nada pendente'}</dd>
      <dt>Remoto</dt><dd class="mono">${esc(g.remote || 'nenhum')}</dd>
      <dt>&Uacute;ltimo</dt><dd>${esc(g.last_msg || '—')}<br><span style="color:var(--faint);font-size:12px">${esc(fmtDate(g.last_ts, true))} &middot; ${esc(g.last_author || '')}</span></dd>
    </dl>

    ${p.dirty && p.dirty.length ? `<div class="section">
      <h3 class="section-title">Altera&ccedil;&otilde;es sem commit</h3>
      ${(() => {
        // Quantas dessas pendências são obra de uma IA. É a pergunta que o
        // número cru "37 arquivos" nunca respondeu: fui eu ou foi ela?
        const daIa = (p.touched || []).filter(a => a.pendente).length;
        return daIa ? `<p class="chart-note" style="margin:0 0 10px">
          <strong>${daIa}</strong> ${daIa === 1 ? 'destes arquivos foi editado' : 'destes arquivos foram editados'}
          por uma IA &mdash; <a href="#" data-goto-arq="1" style="color:var(--claude)">ver quais</a>.</p>` : '';
      })()}
      <div class="list">${p.dirty.slice(0, 60).map(f => `
        <div class="row" style="cursor:default;padding:5px 12px">
          <span class="tag mono" style="flex:0 0 auto">${esc(f.status)}</span>
          <span class="grow ellip mono" style="font-size:12px">${esc(f.path)}</span>
        </div>`).join('')}</div>
      ${actionButton('git-commit', 'Commitar tudo', p.id)}
    </div>` : ''}

    <div class="section">
      <h3 class="section-title">Hist&oacute;rico</h3>
      <div class="list">${(p.commits || []).map(c => `
        <div class="row" style="cursor:default;padding:6px 12px">
          <span class="tag mono" style="flex:0 0 auto">${esc(c.short)}</span>
          <span class="grow ellip">${esc(c.message)}</span>
          <span class="num">${esc(fmtDate(c.ts))}</span>
        </div>`).join('')}</div>
    </div>`;
}

function tabContextos(p) {
  const docs = p.artifacts.filter(a => a.kind === 'context');
  if (!p.context_files.length && !docs.length) {
    return `<div class="empty"><h3>Nenhum arquivo de contexto</h3>
      <p>N&atilde;o h&aacute; CLAUDE.md, AGENTS.md nem planos do Antigravity ligados a este projeto.</p></div>`;
  }
  return `
    ${p.context_files.length ? `<div class="section">
      <h3 class="section-title">No projeto</h3>
      <div class="list">${p.context_files.map(f => `
        <div class="row" style="cursor:default">
          <span class="tag mono" style="flex:0 0 auto">${esc(f.name)}</span>
          <span class="grow ellip" style="color:var(--faint);font-size:12px">${esc(f.scope)}</span>
          <span class="num">${fmtBytes(f.size)} &middot; ${esc(fmtAgo(f.mtime))}</span>
        </div>`).join('')}</div>
    </div>` : ''}
    ${docs.length ? `<div class="section">
      <h3 class="section-title">Gerados pelas IAs</h3>
      <div class="list">${docs.map(a => `
        <div class="row" data-artifact="${a.id}">
          <span class="ai-dot ${esc(a.ai || '')}" style="flex:0 0 7px"></span>
          <span class="grow ellip">${esc(a.name)}</span>
          <span class="num">${esc(fmtAgo(a.mtime))}</span>
        </div>`).join('')}</div>
    </div>` : ''}
    <div id="artifact-view"></div>`;
}

/* A galeria junta DUAS naturezas de imagem:
   · arquivo em disco (generated_images do Codex, .user_uploaded do Antigravity);
   · imagem colada, que vive em base64 dentro do JSONL e é servida por seek.
   Sem a segunda, só apareciam as de saída — que é justamente o que menos importa. */
function imagensDoProjeto(p) {
  const deArquivo = p.artifacts.filter(a => a.kind === 'image').map(a => ({
    id: 'a' + a.id, url: `/api/artifact/${a.id}?raw=1`, nome: a.name,
    ai: a.ai, ts: a.mtime, bytes: a.size,
    // O caminho diz a origem: .user_uploaded é você; generated é a IA.
    origem: /user_uploaded|tempmedia/i.test(a.path || '') ? 'entrada' : 'saida',
  }));
  const embutidas = (p.inline_media || []).map(m => ({
    id: 'm' + m.id, url: `/api/media/${m.id}`, nome: m.media_type || 'imagem',
    ai: m.ai, ts: m.ts, bytes: m.bytes, origem: m.origem,
  }));
  return deArquivo.concat(embutidas).sort((a, b) => (b.ts || 0) - (a.ts || 0));
}

function tabImagens(p) {
  const todas = imagensDoProjeto(p);
  if (!todas.length) {
    return `<div class="empty"><h3>Nenhuma imagem</h3>
      <p>Capturas coladas nas conversas e imagens geradas apareceriam aqui.</p></div>`;
  }
  const filtro = state.filtroImagem || 'todas';
  const vis = todas.filter(i => filtro === 'todas' || i.origem === filtro);
  const nEnt = todas.filter(i => i.origem === 'entrada').length;
  const nSai = todas.length - nEnt;

  return `<div class="filters" style="margin-bottom:14px">
      <button class="chip ${filtro === 'todas' ? 'on' : ''}" data-img="todas">Todas
        <span style="color:var(--faint)">${todas.length}</span></button>
      <button class="chip ${filtro === 'entrada' ? 'on' : ''}" data-img="entrada">Você enviou
        <span style="color:var(--faint)">${nEnt}</span></button>
      <button class="chip ${filtro === 'saida' ? 'on' : ''}" data-img="saida">A IA gerou
        <span style="color:var(--faint)">${nSai}</span></button>
    </div>
    <div class="gallery">${vis.map(i => `
      <a href="${i.url}" target="_blank"
         title="${esc(i.nome)} · ${i.origem === 'entrada' ? 'você enviou' : 'a IA gerou'} · ${esc(fmtDate(i.ts, true))}">
        <img loading="lazy" src="${i.url}" alt="${esc(i.nome)}">
        <span class="gal-tag ${i.origem}">${i.origem === 'entrada' ? 'você' : 'IA'}</span>
      </a>`).join('')}</div>`;
}

function tabSnapshots(p) {
  const lista = p.snapshots || [];
  const cabeca = `<p style="font-size:12.5px;color:var(--muted);margin:0 0 14px">
      C&oacute;pias .zip do projeto, guardadas em <span class="mono">%LOCALAPPDATA%\ProjectCommit\snapshots</span>
      &mdash; fora do projeto, por isso criar um snapshot funciona mesmo no Modo Seguro.
      <strong>Restaurar</strong> escreve por cima do projeto, ent&atilde;o esse sim depende do Modo Seguro
      &mdash; e antes de sobrescrever ele guarda o estado atual.
    </p>`;
  if (!lista.length) {
    return cabeca + `<div class="empty"><h3>Nenhum snapshot ainda</h3>
      <p>Use o bot&atilde;o <strong>Snapshot .zip</strong> no topo desta gaveta.</p></div>`;
  }
  return cabeca + `<div class="list">${lista.map(z => `
    <div class="row" style="cursor:default">
      <div class="grow">
        <div class="ellip mono" style="font-size:12.5px">${esc(z.name)}</div>
        <div style="font-size:11.5px;color:var(--faint)">${esc(fmtDate(z.mtime, true))}</div>
      </div>
      <span class="num">${fmtBytes(z.size)}</span>
      <button class="btn small" data-restore="${esc(z.path)}">Restaurar</button>
    </div>`).join('')}</div>`;
}

function tabCusto(p) {
  if (!p.cost.length) return `<div class="empty"><h3>Sem consumo registrado</h3></div>`;
  const max = Math.max(1, ...p.cost.map(r => r.tok_total || 0));
  return `<table class="data"><thead><tr>
      <th>IA / modelo</th><th class="num">Sess.</th><th class="num">Entrada</th><th class="num">Sa&iacute;da</th>
      <th class="num">Cache grav.</th><th class="num">Total</th><th style="width:110px"></th>
    </tr></thead><tbody>${p.cost.map(r => `<tr>
      <td><span class="tag ${esc(r.ai)}">${esc(AIS[r.ai] ? AIS[r.ai].label : r.ai)}</span>
        <span class="mono" style="font-size:11.5px;color:var(--muted)"> ${esc(shortModel(r.model) || '—')}</span></td>
      <td class="num">${r.n}</td><td class="num">${fmtNum(r.tok_in)}</td><td class="num">${fmtNum(r.tok_out)}</td>
      <td class="num">${fmtNum(r.tok_cache_w)}</td><td class="num"><strong>${fmtNum(r.tok_total)}</strong></td>
      <td><span class="bar"><i style="width:${((r.tok_total || 0) / max * 100).toFixed(1)}%;background:${AIS[r.ai] ? AIS[r.ai].color : 'var(--muted)'}"></i></span></td>
    </tr>`).join('')}</tbody></table>`;
}

function actionButton(action, label, pid) {
  const safe = state.boot && state.boot.settings.safe_mode;
  return `<button class="btn primary" data-action="${action}" data-pid="${pid}" ${safe ? 'disabled' : ''}
    title="${safe ? 'Desligue o Modo Seguro em Configurações para liberar' : ''}"
    style="margin-top:14px">${label}</button>
    ${safe ? '<div class="hint" style="margin-top:7px;font-size:12px;color:var(--faint)">Bloqueado pelo Modo Seguro.</div>' : ''}`;
}

/* ---------- leitor de conversa ---------- */

const PAPEIS = {
  user: 'Você', assistant: 'IA', ferramenta: 'ferramenta',
  contexto: 'contexto', subagente: 'subagente',
};

function wireSessionLinks() {
  ligarAlvos('#drawer [data-sid]', el => openTranscript(el.dataset.sid),
    el => 'Abrir a conversa ' + ((el.querySelector('strong') || {}).textContent || ''));
}

async function openTranscript(sid, offset, roles, at) {
  const tr = state.transcript = {
    sid,
    offset: offset === undefined ? (state.transcript && state.transcript.sid === sid ? state.transcript.offset : 0) : offset,
    roles: roles || (state.transcript && state.transcript.sid === sid ? state.transcript.roles : 'conversa'),
  };
  const body = $('#drawer-body');
  if (body) body.innerHTML = '<div class="sk-line w40"></div>';

  const qs = new URLSearchParams({ sid, offset: tr.offset, limit: 40 });
  // Vindo da busca: o servidor converte a posição no arquivo em página, para
  // que o resultado abra NA mensagem e não no começo da conversa.
  if (at !== undefined && at !== null && at !== '') qs.set('at', at);
  if (tr.roles === 'tudo') qs.set('roles', 'user,assistant,ferramenta,contexto,subagente');
  try {
    tr.data = await api('transcript?' + qs);
    // Conversa grande na primeira abertura: o servidor não segura a resposta por
    // 41 segundos, devolve o tamanho do serviço e indexa em segundo plano.
    if (tr.data.indexando) {
      const ok = await esperarIndexacao(tr.data);
      if (!ok) return;                       // gaveta fechada, ou falhou
      tr.data = await api('transcript?' + qs);
    }
  } catch (err) {
    if (body) body.innerHTML = `<div class="empty"><h3>Não deu para abrir</h3><p>${esc(err.message)}</p></div>`;
    return;
  }
  tr.offset = tr.data.offset;
  drawTranscript();
}

/** Mostra a barra enquanto o servidor indexa, e resolve quando terminar.
 *  Antes disto a gaveta ficava 41 segundos com uma linha de esqueleto de 4px:
 *  sem progresso, sem dizer o que estava acontecendo, sem sair. */
async function esperarIndexacao(info) {
  const body = $('#drawer-body');
  const sid = info.session && info.session.id;
  const nome = (info.session && info.session.title) || 'esta conversa';
  const pintar = (lidos, total) => {
    if (!body) return;
    const pct = total ? Math.min(100, Math.round(lidos / total * 100)) : 0;
    body.innerHTML = `
      <div class="empty" style="padding-top:40px">
        <h3>Preparando a conversa</h3>
        <p>É a primeira vez que ela é aberta, então o programa precisa mapear onde cada
           mensagem começa — <strong>${fmtBytes(info.bytes)}</strong> de histórico.
           Só acontece uma vez; das próximas ela abre na hora.</p>
        <div style="max-width:340px;margin:16px auto 8px;height:6px;border-radius:99px;
                    background:var(--surface-2);overflow:hidden">
          <div id="idx-barra" style="height:100%;width:${pct}%;background:var(--claude);
                    transition:width 300ms linear"></div>
        </div>
        <div style="font-size:12px;color:var(--faint)" id="idx-txt">${pct}% · ${fmtBytes(lidos)}</div>
        <button class="btn small" style="margin-top:14px" id="idx-fechar">Fechar e deixar rodando</button>
      </div>`;
    const fechar = $('#idx-fechar');
    if (fechar) fechar.onclick = () => { state.transcript = null; drawDrawer(); };
  };
  pintar(0, info.bytes);

  for (;;) {
    await new Promise(r => setTimeout(r, 400));
    // Saiu da conversa: a indexação continua no servidor, mas ninguém espera.
    if (!state.transcript || state.transcript.sid !== sid) return false;
    let s;
    try { s = await api('transcript/indexar'); } catch (e) { continue; }
    if (s.erro) {
      if (body) body.innerHTML = `<div class="empty"><h3>Não deu para preparar</h3>
        <p>${esc(s.erro)}</p></div>`;
      return false;
    }
    if (!s.running) return true;
    pintar(s.lidos, s.total || info.bytes);
  }
}

function drawTranscript() {
  const tr = state.transcript, d = tr.data, s = d.session;
  const body = $('#drawer-body');
  if (!body) return;

  if (d.unavailable) {
    body.innerHTML = `${trBar(tr, d, true)}
      <div class="empty"><h3>Conversa não legível</h3><p>${esc(d.unavailable)}</p></div>`;
    wireTranscriptBar();
    return;
  }

  const fim = Math.min(tr.offset + d.messages.length, d.total);
  body.innerHTML = `
    ${trBar(tr, d)}
    <div style="margin-bottom:14px">
      <div style="font-size:12px;color:var(--muted)">
        ${esc(AIS[s.ai] ? AIS[s.ai].label : s.ai)}${s.model ? ' · ' + esc(shortModel(s.model)) : ''}
        · ${esc(fmtDate(s.started_at))} → ${esc(fmtDate(s.ended_at))}
        · ${fmtBytes(s.size_bytes)}
      </div>
    </div>
    ${d.messages.map(m => msgHtml(m, s.ai)).join('') || '<div class="empty"><p>Nada nesta faixa.</p></div>'}
    <div style="display:flex;gap:8px;align-items:center;margin-top:22px">
      <button class="btn small" data-pg="0" ${tr.offset === 0 ? 'disabled' : ''}>Início</button>
      <button class="btn small" data-pg="${Math.max(0, tr.offset - 40)}" ${tr.offset === 0 ? 'disabled' : ''}>← Anteriores</button>
      <span class="tr-count grow" style="text-align:center">${tr.offset + 1}–${fim} de ${d.total}</span>
      <button class="btn small" data-pg="${tr.offset + 40}" ${fim >= d.total ? 'disabled' : ''}>Próximas →</button>
      <button class="btn small" data-pg="${Math.max(0, d.total - 40)}" ${fim >= d.total ? 'disabled' : ''}>Fim</button>
    </div>`;
  wireTranscriptBar();
  $$('#drawer-body [data-pg]').forEach(el => el.onclick = () =>
    openTranscript(tr.sid, Number(el.dataset.pg)));
  body.scrollTop = 0;
}

function trBar(tr, d, simples) {
  return `<div class="tr-bar">
      <button class="btn small" id="tr-voltar">← Sessões</button>
      ${simples ? '' : `
      <button class="chip ${tr.roles === 'conversa' ? 'on' : ''}" data-roles="conversa">Só conversa</button>
      <button class="chip ${tr.roles === 'tudo' ? 'on' : ''}" data-roles="tudo">Tudo</button>
      <span class="tr-count grow" style="text-align:right">${d.total} de ${d.raw_total} registros</span>
      <button class="btn small" id="tr-exportar" title="Salva a conversa inteira em Markdown">Exportar .md</button>`}
    </div>`;
}

function wireTranscriptBar() {
  const voltar = $('#tr-voltar');
  if (voltar) voltar.onclick = () => { state.transcript = null; drawDrawer(); };
  $$('#drawer-body [data-roles]').forEach(el => el.onclick = () =>
    openTranscript(state.transcript.sid, 0, el.dataset.roles));
  const exportar = $('#tr-exportar');
  if (exportar) exportar.onclick = async () => {
    exportar.disabled = true;
    exportar.textContent = 'Exportando…';
    const out = await runAction('transcript/export',
      { sid: state.transcript.sid, tudo: state.transcript.roles === 'tudo' },
      o => `${o.messages} mensagens salvas em ${o.path}`);
    exportar.disabled = false;
    exportar.textContent = 'Exportar .md';
    if (out) console.log('exportado:', out.path);
  };
}

function msgHtml(m, ai) {
  /* A cor do nome de quem falou segue a IA da sessão -- o leitor agora abre
     conversa das três, e não só do Claude. */
  const classeIa = m.role === 'assistant' && (ai === 'codex' || ai === 'antigravity')
    ? ' ' + ai : '';
  const hora = m.ts
    ? new Date(m.ts * 1000).toLocaleString('pt-BR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
    : '';
  return `<div class="msg ${esc(m.role)}${classeIa}">
      <div class="msg-who">${esc(PAPEIS[m.role] || m.role)}</div>
      <div class="msg-body">
        ${m.text ? `<div class="msg-text">${esc(m.text)}</div>` : ''}
        ${m.tools && m.tools.length
          ? `<div class="msg-tools">${m.tools.map(t => `<span>${esc(t)}</span>`).join('')}</div>` : ''}
        ${m.truncated ? '<div class="msg-cut">mensagem cortada — muito longa para exibir inteira</div>' : ''}
        ${hora ? `<div class="msg-time">${esc(hora)}</div>` : ''}
      </div>
    </div>`;
}

function wireArtifactLinks() {
  $$('#drawer [data-artifact]').forEach(el => el.onclick = async () => {
    const box = $('#artifact-view');
    if (!box) return;
    box.innerHTML = '<div class="sk-line w40"></div>';
    try {
      const doc = await api('artifact/' + el.dataset.artifact);
      box.innerHTML = `<div class="section"><h3 class="section-title">${esc(doc.name)}</h3>
        <pre class="code">${esc(doc.text)}</pre></div>`;
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (err) {
      box.innerHTML = `<p style="color:var(--danger)">${esc(err.message)}</p>`;
    }
  });
}

/* ---------- barra lateral ao vivo ---------- */

function drawLiveSidebar() {
  const box = $('#sidebar-live'), list = $('#live-list');
  const todas = (state.radar && state.radar.sessions) || [];
  // Uma linha por PROJETO. A lista vinha por sessão, então duas conversas do
  // Codex no mesmo projeto apareciam como duas entradas idênticas.
  const vistos = new Set();
  const sessions = todas.filter(s => {
    const chave = s.project_id || s.session_id;
    if (vistos.has(chave)) return false;
    vistos.add(chave);
    return true;
  });
  if (!sessions.length) { box.hidden = true; return; }
  box.hidden = false;
  list.innerHTML = sessions.slice(0, 8).map(s => `
    <div class="live-item" ${s.project_id ? `data-id="${s.project_id}"` : ''}>
      <span class="pulse" style="background:${AIS[s.ai] ? AIS[s.ai].color : 'var(--live)'}"></span>
      <div class="lm">
        <div class="lname">${esc(s.project_name || 'sem projeto')}</div>
        <div class="lmeta">${esc(AIS[s.ai] ? AIS[s.ai].label : s.ai)} &middot; ${esc(fmtAgo(s.last_seen))}</div>
      </div>
    </div>`).join('');
  ligarAlvos('#live-list .live-item[data-id]', el => openProject(Number(el.dataset.id)),
    el => 'Abrir ' + ((el.querySelector('.lname') || {}).textContent || 'projeto'));
}

/* ---------- varredura ---------- */

/** O Radar não pode se redesenhar por baixo de quem está digitando no filtro. */
/** Troca a URL SEM disparar `hashchange` — quem redesenha é quem chamou.
 *
 *  `novoPasso` separa as duas intenções: abrir um projeto É um passo de
 *  história (para o Voltar fechar a gaveta), trocar de aba NÃO é (senão o
 *  Voltar percorreria cada aba que você visitou antes de sair do projeto).
 *  Atribuir a `location.hash` direto dispararia navigate() de novo. */
function irPara(hash, novoPasso) {
  if (location.hash === hash) return;
  if (novoPasso) history.pushState(null, '', hash);
  else history.replaceState(null, '', hash);
}

function digitandoNoFiltro() {
  const a = document.activeElement;
  return !!a && a.id === 'filtro-projeto';
}

function pollScan(aoTerminar) {
  clearInterval(state.scanTimer);
  state.scanTimer = setInterval(async () => {
    let s;
    try { s = await api('scan'); } catch (e) { return; }
    const box = $('#scan-status');
    const icon = $('#btn-scan .ico-refresh');
    if (s.running) {
      icon.classList.add('spinning');
      const pct = s.total ? ` ${Math.round(s.done / s.total * 100)}%` : '';
      box.textContent = (s.label || 'varrendo') + pct;
    } else {
      icon.classList.remove('spinning');
      clearInterval(state.scanTimer);
      box.textContent = s.error ? 'falhou' : 'índice atualizado';
      if (s.error) toast('Varredura falhou: ' + s.error, 'err');
      await refreshBoot();
      if (typeof aoTerminar === 'function') aoTerminar();
      // Mesma guarda do temporizador de 20s: redesenhar o Radar enquanto a
      // pessoa digita no filtro tira o cursor do campo no meio da palavra.
      // Sem isto acontecia a cada 3 minutos, quando a varredura automática
      // terminava.
      else if (state.view === 'radar' && !digitandoNoFiltro()) { await loadRadar(); drawRadar(); }
      setTimeout(() => { if (!$('#scan-status').textContent.includes('%')) $('#scan-status').textContent = ''; }, 4000);
    }
  }, 400);
}

/* O índice envelhecia com a janela aberta: a varredura só rodava ao abrir e no
   R. O selo "ao vivo" continuava certo porque lê o mtime direto do disco, mas
   prompt novo e sessão nova não apareciam.

   Medido: incremental SEM git leva 0,58 s; o git sozinho custa 2 s e muda pouco.
   Daí a cadência desigual — de 3 em 3 minutos sem git, e a cada quinta vez (15
   min) com ele. */
const VARREDURA_MS = 3 * 60 * 1000;
const VARREDURA_COM_GIT_A_CADA = 5;

function varreduraAutomatica() {
  let voltas = 0;
  state.autoScanTimer = setInterval(async () => {
    // Janela escondida não precisa de índice fresco, e uma varredura no meio de
    // uma gaveta aberta redesenharia por baixo de quem está lendo.
    if (document.hidden || !$('#drawer').hidden || state.transcript) return;
    try {
      if ((await api('scan')).running) return;
      voltas += 1;
      await post('scan', { git: voltas % VARREDURA_COM_GIT_A_CADA === 0 });
      pollScan();
    } catch (err) { /* servidor ocupado: a próxima volta tenta de novo */ }
  }, VARREDURA_MS);
}

async function refreshBoot() {
  try {
    state.boot = await api('bootstrap');
    const chip = $('#safe-mode-chip');
    const safe = state.boot.settings.safe_mode;
    chip.className = 'safe-mode' + (safe ? '' : ' off');
    chip.innerHTML = `<span class="sm-dot"></span>${safe ? 'Modo Seguro' : 'Escrita liberada'}`;
  } catch (err) { /* servidor ainda subindo */ }
}

/* ---------- ajuda dos atalhos ---------- */

const ATALHOS = [
  ['1 – 6', 'trocar de tela'],
  ['Ctrl K', 'buscar em tudo'],
  ['R', 'atualizar o índice'],
  ['T', 'alternar tema'],
  ['Esc', 'fechar a gaveta / voltar da conversa'],
  ['?', 'mostrar esta lista'],
];

function alternarAjuda() {
  const existente = $('#ajuda');
  if (existente) { existente.remove(); return; }
  const div = document.createElement('div');
  div.id = 'ajuda';
  div.className = 'ajuda-box';
  div.innerHTML = `<div class="sidebar-label" style="padding:0;margin-bottom:9px">Atalhos</div>
    ${ATALHOS.map(([k, d]) => `<div class="ajuda-linha"><kbd>${esc(k)}</kbd><span>${esc(d)}</span></div>`).join('')}`;
  document.body.appendChild(div);
  setTimeout(() => document.addEventListener('click', function fecha() {
    const el = $('#ajuda'); if (el) el.remove();
    document.removeEventListener('click', fecha);
  }), 0);
}

/* ---------- inicializacao ---------- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('pc-theme', theme); } catch (e) { /* sem storage */ }
}

async function boot() {
  // ?tema=light|dark força o tema sem depender do localStorage -- serve para
  // conferir o tema claro numa captura automatizada.
  const temaUrl = new URLSearchParams(location.search).get('tema');
  try {
    applyTheme(temaUrl === 'light' || temaUrl === 'dark'
      ? temaUrl : (localStorage.getItem('pc-theme') || 'dark'));
  } catch (e) { applyTheme(temaUrl || 'dark'); }

  $('#btn-ajuda').onclick = ev => { ev.stopPropagation(); alternarAjuda(); };

  $('#btn-theme').onclick = () =>
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

  $('#btn-scan').onclick = async () => {
    await post('scan', {});
    toast('Atualizando o índice…');
    pollScan();
  };

  const search = $('#global-search');
  let searchTimer = null;
  search.oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      const q = search.value.trim();
      if (q.length >= 2) location.hash = '#/busca/' + encodeURIComponent(q);
      else if (state.view === 'busca') location.hash = '#/radar';
    }, 260);
  };

  document.addEventListener('keydown', ev => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k') { ev.preventDefault(); search.focus(); search.select(); }

    // Atalhos só quando o foco não está num campo de texto.
    const digitando = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || '');
    if (!digitando && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
      const n = parseInt(ev.key, 10);
      if (n >= 1 && n <= TELAS.length) { location.hash = '#/' + TELAS[n - 1]; return; }
      if (ev.key === 'r' || ev.key === 'R') { $('#btn-scan').click(); return; }
      if (ev.key === 't' || ev.key === 'T') { $('#btn-theme').click(); return; }
      if (ev.key === '?') { alternarAjuda(); return; }
    }
    if (ev.key === 'Escape') {
      if (state.transcript) { state.transcript = null; drawDrawer(); }
      else if (!$('#drawer').hidden) closeDrawer();
      else if (document.activeElement === search) search.blur();
    }
  });

  $('#drawer-backdrop').onclick = closeDrawer;
  window.addEventListener('hashchange', navigate);

  // Os gráficos são desenhados para uma largura fixa, então mudar o tamanho da
  // janela exige redesenhar -- senão eles ficam sobrando ou faltando espaço.
  let tRedim = null;
  window.addEventListener('resize', () => {
    clearTimeout(tRedim);
    tRedim = setTimeout(() => { if (state.view === 'consumo') VIEWS.consumo(); }, 220);
  });

  await refreshBoot();
  navigate();
  pollScan();

  // O radar respira sozinho: o selo ao vivo nao pode depender de F5.
  state.liveTimer = setInterval(async () => {
    if (state.view !== 'radar' || !$('#drawer').hidden) return;
    if (digitandoNoFiltro()) return;
    await loadRadar();
    drawRadar();
  }, 20000);

  varreduraAutomatica();
}

boot();
