/* Gráficos em SVG puro — sem biblioteca.
 *
 * Regras seguidas aqui, em vez de gosto:
 *  · a cor segue a IA, nunca a posição no ranking;
 *  · um eixo por gráfico, jamais dois;
 *  · grade em fio sólido fino (tracejado lê como "projeção");
 *  · rótulo em cor de texto — a marca colorida ao lado carrega a identidade;
 *  · rótulo direto só no extremo, nunca um número em cada ponto;
 *  · folga de 2px na cor da superfície separando marcas, não uma borda;
 *  · todo gráfico tem tabela equivalente, porque cor sozinha não é acessível.
 */

'use strict';

const DIAS = ['segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado', 'domingo'];
const DIAS_CURTO = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom'];

/* ---------- dica flutuante compartilhada ---------- */

let _tip;
function tipEl() {
  if (!_tip) {
    _tip = document.createElement('div');
    _tip.className = 'chart-tip';
    document.body.appendChild(_tip);
  }
  return _tip;
}

function mostrarTip(ev, html) {
  const t = tipEl();
  t.innerHTML = html;
  t.classList.add('on');
  const r = t.getBoundingClientRect();
  const x = Math.min(ev.clientX + 14, window.innerWidth - r.width - 8);
  const y = Math.max(8, ev.clientY - r.height - 12);
  t.style.left = x + 'px';
  t.style.top = y + 'px';
}

function esconderTip() {
  if (_tip) _tip.classList.remove('on');
}

/** Liga a dica em qualquer marca que tenha data-tip. Alvo maior que a marca. */
function ligarTips(raiz) {
  raiz.querySelectorAll('[data-tip]').forEach(el => {
    el.addEventListener('mousemove', ev => mostrarTip(ev, el.dataset.tip));
    el.addEventListener('mouseleave', esconderTip);
    el.addEventListener('focus', ev => mostrarTip(
      { clientX: el.getBoundingClientRect().left, clientY: el.getBoundingClientRect().top }, el.dataset.tip));
    el.addEventListener('blur', esconderTip);
  });
}

/* ---------- utilidades ---------- */

const esc2 = s => String(s === null || s === undefined ? '' : s)
  .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function compacto(n) {
  n = Number(n || 0);
  if (n >= 1e9) return (n / 1e9).toFixed(1).replace('.', ',') + ' bi';
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace('.', ',') + ' mi';
  // Abaixo de 10 mil o valor sai por extenso: compactar 1069 para "1 mil"
  // joga fora precisao justamente onde o numero ainda cabe na tela.
  if (n >= 1e4) return Math.round(n / 1e3) + ' mil';
  return n.toLocaleString('pt-BR');
}

/** Escadinha de valores redondos para o eixo. */
function ticks(max, n = 4) {
  if (max <= 0) return [0];
  const bruto = max / n;
  const mag = Math.pow(10, Math.floor(Math.log10(bruto)));
  const passo = [1, 2, 2.5, 5, 10].map(m => m * mag).find(p => p >= bruto) || mag * 10;
  const out = [];
  for (let v = 0; v <= max * 1.0001; v += passo) out.push(v);
  return out;
}

/* ---------- 1. Mapa de ritmo (dia da semana × hora) ---------- */

function mapaRitmo(grade, opts = {}) {
  const rotuloW = 34, topo = 18, alturaEixo = 16;
  // A célula é calculada a partir da largura REAL do container, para o SVG sair
  // 1:1 com os pixels da tela. Sem isto o navegador estica o viewBox e a fonte
  // de 10px do eixo aparece a 16px -- tipografia diferente do resto da interface.
  const disp = Math.max(320, (opts.largura || 562) - 2);
  const cel = Math.max(14, Math.min(34, Math.floor((disp - rotuloW) / 24)));
  const w = rotuloW + 24 * cel;
  const h = topo + 7 * cel + alturaEixo;
  const max = Math.max(1, ...grade.flat());

  // Degraus por QUANTIL, não por fração do máximo.
  // Escala linear aqui mentia: com um pico de 435 e mediana perto de 5, 95% das
  // células caíam no degrau 1 e o mapa virava quase binário. Cortando pelos
  // quintis, cada degrau recebe um quinto das horas com atividade e o desenho
  // volta a mostrar a variação real do ritmo.
  const naoZero = grade.flat().filter(v => v > 0).sort((a, b) => a - b);
  const cortes = [1, 2, 3, 4].map(i => naoZero[Math.floor(naoZero.length * i / 5)] || 1);
  const passo = v => {
    if (v === 0) return 'var(--heat-0)';
    let d = 1;
    while (d <= 4 && v > cortes[d - 1]) d += 1;
    return 'var(--heat-' + d + ')';
  };

  let corpo = '';
  for (let d = 0; d < 7; d++) {
    corpo += `<text class="eixo" x="${rotuloW - 7}" y="${topo + d * cel + cel / 2 + 3}"
                text-anchor="end">${DIAS_CURTO[d]}</text>`;
    for (let hh = 0; hh < 24; hh++) {
      const v = grade[d][hh];
      corpo += `<rect class="heat-cel" x="${rotuloW + hh * cel}" y="${topo + d * cel}"
        width="${cel}" height="${cel}" rx="3" fill="${passo(v)}" tabindex="0"
        data-tip="<b>${DIAS[d]}, ${String(hh).padStart(2, '0')}h</b>
                  <span class='val'>${v} ${v === 1 ? 'mensagem' : 'mensagens'}</span>"/>`;
    }
  }
  // Hora só de 3 em 3, senão os rótulos colidem.
  for (let hh = 0; hh < 24; hh += 3) {
    corpo += `<text class="eixo" x="${rotuloW + hh * cel + cel / 2}" y="${h - 4}"
                text-anchor="middle">${String(hh).padStart(2, '0')}</text>`;
  }

  return `<svg class="chart-svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
      aria-label="${esc2(opts.aria || 'Mensagens por dia da semana e hora')}">${corpo}</svg>`;
}

/** Legenda da rampa. Mostra os CORTES, não só o máximo: com escala por quantil
 *  "até 435" não diria onde cada tom começa. */
function escalaCalor(grade) {
  const naoZero = grade.flat().filter(v => v > 0).sort((a, b) => a - b);
  if (!naoZero.length) return '';
  const cortes = [1, 2, 3, 4].map(i => naoZero[Math.floor(naoZero.length * i / 5)] || 1);
  const faixas = [
    '1–' + cortes[0], (cortes[0] + 1) + '–' + cortes[1], (cortes[1] + 1) + '–' + cortes[2],
    (cortes[2] + 1) + '–' + cortes[3], '> ' + cortes[3],
  ];
  return `<div class="heat-escala">${[1, 2, 3, 4, 5].map((i, k) =>
      `<span class="heat-passo"><i style="background:var(--heat-${i})"></i>${esc2(faixas[k])}</span>`).join('')}
    <span style="color:var(--faint)">mensagens/hora</span></div>`;
}

/* ---------- 2. Barras verticais por dia ---------- */

function barrasDia(pontos, opts = {}) {
  // pontos: [{rotulo, valor, dica}]
  const w = Math.max(360, opts.largura || 720), h = 190, esq = 44, dir = 8, topo = 12, base = h - 26;
  const max = Math.max(1, ...pontos.map(p => p.valor));
  const escala = ticks(max);
  const alvoMax = escala[escala.length - 1];
  const larg = (w - esq - dir) / Math.max(pontos.length, 1);
  const barra = Math.max(2, larg - 2);           // folga de 2px entre vizinhas

  let corpo = '';
  escala.forEach(v => {
    const y = base - (v / alvoMax) * (base - topo);
    corpo += `<line class="grade" x1="${esq}" y1="${y}" x2="${w - dir}" y2="${y}"/>
              <text class="eixo" x="${esq - 7}" y="${y + 3}" text-anchor="end">${compacto(v)}</text>`;
  });

  const iMax = pontos.reduce((m, p, i) => p.valor > pontos[m].valor ? i : m, 0);
  pontos.forEach((p, i) => {
    const alt = (p.valor / alvoMax) * (base - topo);
    const x = esq + i * larg;
    const y = base - alt;
    corpo += `<rect class="marca" x="${x}" y="${y}" width="${barra}" height="${Math.max(alt, 0)}"
      rx="3" fill="${opts.cor || 'var(--chart-1)'}" tabindex="0" data-tip="${esc2(p.dica)}"/>`;
    if (i === iMax && alt > 14) {
      // Rótulo direto só no pico — número em toda barra vira ruído.
      corpo += `<text class="rotulo" x="${x + barra / 2}" y="${y - 5}" text-anchor="middle">${compacto(p.valor)}</text>`;
    }
  });

  const cada = Math.ceil(pontos.length / 8) || 1;
  pontos.forEach((p, i) => {
    if (i % cada) return;
    corpo += `<text class="eixo" x="${esq + i * larg + barra / 2}" y="${h - 8}"
                text-anchor="middle">${esc2(p.rotulo)}</text>`;
  });

  return `<svg class="chart-svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
      aria-label="${esc2(opts.aria || 'Valores por dia')}">${corpo}</svg>`;
}

/* ---------- 3. Barras horizontais (uma por item) ---------- */

function barrasHoriz(itens, opts = {}) {
  // itens: [{rotulo, valor, cor, dica}]
  const linha = 26, rotuloW = opts.rotuloW || 190, dir = 58;
  const w = Math.max(360, opts.largura || 720), h = itens.length * linha + 8;
  const max = Math.max(1, ...itens.map(i => i.valor));
  const util = w - rotuloW - dir;

  let corpo = '';
  itens.forEach((it, i) => {
    const y = i * linha + 4;
    const larg = Math.max(2, (it.valor / max) * util);
    corpo += `
      <text class="eixo" x="${rotuloW - 9}" y="${y + 13}" text-anchor="end"
        style="font-size:11px;fill:var(--text-2)">${esc2(it.rotulo)}</text>
      <rect class="marca" x="${rotuloW}" y="${y + 3}" width="${larg}" height="14" rx="3"
        fill="${it.cor || 'var(--chart-1)'}" tabindex="0" data-tip="${esc2(it.dica)}"/>
      <text class="eixo" x="${rotuloW + larg + 7}" y="${y + 14}">${compacto(it.valor)}</text>`;
  });
  return `<svg class="chart-svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
      aria-label="${esc2(opts.aria || 'Comparação por item')}">${corpo}</svg>`;
}

/* ---------- 4. Linha de vida do projeto ---------- */

/* Linha de vida: uma faixa de DENSIDADE por IA, não uma barra por sessão.
 *
 * A versão anterior desenhava cada sessão como um retângulo do início ao fim.
 * Medido num projeto de 138 sessões: numa faixa de 604px elas somavam 1.277px --
 * 211% de sobreposição --, e a sessão mediana caía na largura mínima de 3px.
 * O resultado era uma mancha verde sólida. Além disso o eixo trazia duas datas
 * para 96 dias de intervalo.
 *
 * Agora o tempo é fatiado em colunas e cada coluna é pintada pela QUANTIDADE de
 * trabalho naquele pedaço, usando a mesma rampa do mapa de calor (que já passou
 * pelo validador de paleta). A pergunta que o gráfico responde continua a mesma
 * -- "quando houve trabalho, e quando isso virou commit?" -- mas agora ela é
 * legível com 1 sessão ou com 500. */
function linhaDeVida(sessoes, commits, opts = {}) {
  const w = Math.max(320, opts.largura || 660);
  const marcos = commits || [];
  const tempos = sessoes.flatMap(s => [s.inicio, s.fim]).concat(marcos.map(c => c.ts)).filter(Boolean);
  if (!tempos.length) return '';

  const t0 = Math.min(...tempos), t1 = Math.max(...tempos);
  const span = Math.max(t1 - t0, 3600);
  const esq = 8, dir = 8, faixaW = w - esq - dir;
  const x = t => esq + ((t - t0) / span) * faixaW;

  // Uma coluna por período, com ~5px cada: fino o bastante para mostrar forma,
  // largo o bastante para a cor ser visível.
  const colunas = Math.max(12, Math.min(160, Math.floor(faixaW / 5)));
  const passo = span / colunas;
  const colW = faixaW / colunas;
  const periodo = passo >= 86400 * 6 ? 'semana' : passo >= 3600 * 20 ? 'dia' : 'hora';

  const ias = [...new Set(sessoes.map(s => s.ai))];
  const alturaFaixa = 18, topo = 15, base = 36;
  const h = topo + ias.length * alturaFaixa + base;

  // Contagem por (IA, coluna). Uma sessão longa pinta todas as colunas que cruza.
  const balde = new Map();
  let pico = 0;
  sessoes.forEach(s => {
    const ini = Math.max(0, Math.floor((s.inicio - t0) / passo));
    const fim = Math.min(colunas - 1, Math.floor(((s.fim || s.inicio) - t0) / passo));
    for (let c = ini; c <= fim; c++) {
      const k = s.ai + ' ' + c;
      const n = (balde.get(k) || 0) + 1;
      balde.set(k, n);
      if (n > pico) pico = n;
    }
  });

  let corpo = '';
  ias.forEach((ia, i) => {
    const y = topo + i * alturaFaixa;
    corpo += `<text class="eixo" x="${esq}" y="${y - 2}">${esc2(ia)}</text>`;
    for (let c = 0; c < colunas; c++) {
      const n = balde.get(ia + ' ' + c) || 0;
      if (!n) continue;
      // 5 degraus, como a legenda do mapa de calor.
      const grau = Math.min(5, Math.max(1, Math.ceil(n / Math.max(pico, 1) * 5)));
      const ini = new Date((t0 + c * passo) * 1000);
      const quando = periodo === 'hora'
        ? ini.toLocaleString('pt-BR', { day: '2-digit', month: 'short', hour: '2-digit' })
        : ini.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' });
      corpo += `<rect class="vida-barra" x="${(esq + c * colW).toFixed(1)}" y="${y}"
        width="${Math.max(colW - 0.6, 1.4).toFixed(1)}" height="11" rx="1.5"
        fill="${ia ? 'var(--heat-' + grau + ')' : 'var(--muted)'}" tabindex="0"
        data-tip="${esc2('<b>' + quando + '</b><span class="val">' + n +
          (n === 1 ? ' sessão' : ' sessões') + ' &middot; por ' + periodo + '</span>')}"/>`;
    }
  });

  const yCommits = topo + ias.length * alturaFaixa + 10;
  marcos.forEach(c => {
    corpo += `<line class="vida-commit" x1="${x(c.ts).toFixed(1)}" y1="${yCommits}" x2="${x(c.ts).toFixed(1)}" y2="${yCommits + 10}"
      tabindex="0" data-tip="${esc2(c.dica)}"/>`;
  });

  // Três marcas no eixo em vez de duas: com 96 dias de intervalo, saber só as
  // pontas não diz onde o trabalho se concentrou.
  const fmt = t => new Date(t * 1000).toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' });
  const meio = t0 + span / 2;
  corpo += `<line class="grade" x1="${esq}" y1="${h - 15}" x2="${w - dir}" y2="${h - 15}"/>
    <text class="eixo" x="${esq}" y="${h - 3}">${fmt(t0)}</text>
    <text class="eixo" x="${(esq + faixaW / 2).toFixed(0)}" y="${h - 3}" text-anchor="middle">${fmt(meio)}</text>
    <text class="eixo" x="${w - dir}" y="${h - 3}" text-anchor="end">${fmt(t1)}</text>`;

  return `<svg class="chart-svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
      aria-label="${esc2(opts.aria || 'Linha de vida do projeto')}">${corpo}</svg>`;
}

/* ---------- tabela equivalente ---------- */

/** Todo gráfico ganha um par em tabela: cor sozinha não é encoding acessível. */
function tabelaDe(colunas, linhas) {
  return `<table class="data" style="margin-top:8px">
    <thead><tr>${colunas.map((c, i) =>
      `<th class="${i ? 'num' : ''}">${esc2(c)}</th>`).join('')}</tr></thead>
    <tbody>${linhas.map(l => `<tr>${l.map((v, i) =>
      `<td class="${i ? 'num' : ''}">${esc2(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

/** Cartão com o gráfico e o botão que troca para a tabela. */
function cartao(id, titulo, nota, svg, tabela, extra) {
  return `<div class="chart-card">
    <div class="chart-head">
      <h3 class="chart-title">${esc2(titulo)}</h3>
      <div class="spacer"></div>
      ${extra || ''}
      <button class="btn small ghost" data-tabela="${id}">Ver tabela</button>
    </div>
    ${nota ? `<p class="chart-note">${nota}</p>` : ''}
    <div id="g-${id}">${svg}</div>
    <div id="t-${id}" hidden>${tabela}</div>
  </div>`;
}

function ligarTabelas(raiz) {
  raiz.querySelectorAll('[data-tabela]').forEach(b => b.onclick = () => {
    const g = raiz.querySelector('#g-' + b.dataset.tabela);
    const t = raiz.querySelector('#t-' + b.dataset.tabela);
    const mostrandoTabela = g.hidden;
    g.hidden = !mostrandoTabela;
    t.hidden = mostrandoTabela;
    b.textContent = mostrandoTabela ? 'Ver tabela' : 'Ver gráfico';
  });
}

window.Charts = {
  mapaRitmo, escalaCalor, barrasDia, barrasHoriz, linhaDeVida,
  tabelaDe, cartao, ligarTabelas, ligarTips, compacto, DIAS, DIAS_CURTO,
};
