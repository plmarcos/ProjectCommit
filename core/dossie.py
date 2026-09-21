"""Dossie do projeto: tudo que o painel sabe, em Markdown, para outra IA ler.

O problema que ele resolve: voce abre uma conversa nova com outra IA e ela nao
sabe nada -- onde o projeto fica, quem mexeu, o que ficou pendente, quais metas
existem. Voce cola o dossie e ela comeca sabendo.

Ele tambem e' o que FECHA o ciclo das metas. O painel nao inventa meta e nao
guarda meta: ele le' `ROADMAP.md`. Como quase nenhum projeto tem esse arquivo, o
dossie termina pedindo que a IA o mantenha -- e na proxima varredura o painel ja'
mostra a porcentagem, com o numero sendo seu, nao chutado pelo programa.

A ordem das secoes e' a ordem em que alguem que nao conhece o projeto precisa:
onde fica, o que e', o que esta' pendente, o que falta, o que ja' se conversou.
"""
from __future__ import annotations

import os
import time

from . import metas as _metas
from . import settings, util

#: Conteudo grande NAO entra: um `CLAUDE.md` pode ter 33 KB, e a IA le' o arquivo
#: direto, com o caminho que o dossie da'. Duplicar so' gastaria contexto.
_MAX_SESSOES = 12
_MAX_ARQUIVOS = 25
_MAX_METAS_ABERTAS = 40


def _quando(ts, com_hora=False):
    if not ts:
        return "—"
    f = "%d/%m/%Y %H:%M" if com_hora else "%d/%m/%Y"
    return time.strftime(f, time.localtime(ts))


def montar(projeto):
    """Markdown completo a partir do payload de /api/project/<id>."""
    p = projeto
    L = []
    w = L.append

    w("# %s" % (p.get("name") or "projeto"))
    w("")
    w("> Dossiê gerado pelo ProjectCommit em %s." % _quando(util.now(), True))
    w("> Ele reúne o que três IAs já fizeram neste projeto. Leia antes de mexer.")
    w("")
    w("- **Pasta:** `%s`" % p.get("path"))
    ias = p.get("ais") or sorted({s.get("ai") for s in p.get("sessions") or [] if s.get("ai")})
    if ias:
        w("- **IAs que trabalharam aqui:** %s" % ", ".join(ias))
    w("- **Sessões registradas:** %d" % len(p.get("sessions") or []))
    if not p.get("on_disk", 1):
        w("- **Atenção:** esta pasta não existe mais no disco.")
    w("")

    _pastas(w, p)
    _git(w, p)
    _metas_secao(w, p)
    _sinais(w, p)
    _arquivos(w, p)
    _contexto(w, p)
    _sessoes(w, p)
    _fechamento(w, p)
    return "\n".join(L).rstrip() + "\n"


def _pastas(w, p):
    pastas = p.get("pastas") or {}
    dentro, fora = pastas.get("dentro") or [], pastas.get("fora") or []
    if not dentro and not fora:
        return
    w("## Onde o trabalho acontece")
    w("")
    if dentro:
        w("Subpastas deste projeto que as IAs editaram:")
        w("")
        w("| Pasta | Arquivos | Escritas |")
        w("|---|---:|---:|")
        for b in dentro[:12]:
            w("| `%s` | %d | %d |" % (b["nome"], b["arquivos"], b["escritas"]))
        w("")
    if fora:
        w("**Fora desta pasta** — sessões deste projeto também escreveram em:")
        w("")
        for b in fora[:8]:
            w("- `%s` — %d arquivo%s" % (b["nome"], b["arquivos"], "s" if b["arquivos"] > 1 else ""))
        w("")


def _git(w, p):
    g = p.get("git") or {}
    w("## Versionamento")
    w("")
    if not g.get("has_git"):
        tocados = (p.get("touched") or {}).get("total") or 0
        w("**Este projeto não tem repositório git.**"
          + (" As IAs já escreveram em %d arquivos aqui, e não há como voltar atrás."
             % tocados if tocados else ""))
        w("")
        return
    w("- Branch: `%s` · %s commits" % (g.get("branch") or "?", g.get("commit_count")))
    if g.get("remote"):
        w("- Remoto: `%s`" % g["remote"])
    w("- Último commit: %s — %s"
      % (_quando(g.get("last_ts")), util.clip(g.get("last_msg"), 110) or "—"))
    t = p.get("touched") or {}
    if t.get("pendentes"):
        w("- **%d arquivos editados por IA estão fora do git** (%d nunca commitados)."
          % (t["pendentes"], t.get("novos") or 0))
    w("")


def _metas_secao(w, p):
    m = p.get("metas") or {}
    w("## Metas")
    w("")
    if not m.get("tem"):
        w("**Este projeto não declara metas.** Não há `ROADMAP.md` (nem `TODO.md`,")
        w("`PLANO.md`, `task.md`) com caixas de marcação, então o painel não tem o que medir")
        w("— e por isso não mostra barra de progresso nenhuma.")
        w("")
        w("Se quiser acompanhar o andamento, crie um destes arquivos neste formato:")
        w("")
        w("```markdown")
        w(_metas.EXEMPLO.rstrip())
        w("```")
        w("")
        w("O `[~]` vale meio ponto, para o que está pela metade.")
        w("")
        return
    w("**%d%% concluído** — %d metas em `%s`: %d feitas, %d parciais, %d abertas."
      % (m["percentual"], m["total"], ", ".join(m.get("arquivos") or []),
         m["feitas"], m["parciais"], m["abertas"]))
    w("")
    abertas = [
        (g["titulo"], i) for g in m.get("grupos") or []
        for i in g["itens"] if i["estado"] != "feita"
    ]
    if abertas:
        w("### O que ainda falta")
        w("")
        atual = None
        for titulo, i in abertas[:_MAX_METAS_ABERTAS]:
            if titulo != atual:
                atual = titulo
                w("")
                w("**%s**" % titulo)
            w("- [%s] %s" % ("~" if i["estado"] == "parcial" else " ", i["texto"]))
        if len(abertas) > _MAX_METAS_ABERTAS:
            w("")
            w("_(e mais %d)_" % (len(abertas) - _MAX_METAS_ABERTAS))
    else:
        w("Todas as metas declaradas estão concluídas.")
    w("")


def _sinais(w, p):
    s = p.get("sinais") or {}
    if not s.get("tem"):
        return
    n = s.get("por_nivel") or {}
    w("## Marcadores deixados no código")
    w("")
    w("%d marcador%s nos arquivos que as IAs editam — %s."
      % (s["total"], "es" if s["total"] > 1 else "",
         ", ".join("%d %s" % (v, k) for k, v in sorted(n.items()))))
    w("Isto **não** é uma contagem de bugs: é o que está escrito em comentário no código.")
    w("")
    for i in (s.get("itens") or [])[:20]:
        w("- **%s** `%s:%d` — %s" % (i["marca"], i["rel"], i["linha"], i.get("texto") or ""))
    w("")


def _arquivos(w, p):
    t = p.get("touched") or {}
    itens = t.get("itens") or []
    if not itens:
        return
    w("## Arquivos que as IAs mais editam")
    w("")
    w("| Arquivo | Escritas | Sessões | Estado |")
    w("|---|---:|---:|---|")
    for i in sorted(itens, key=lambda x: -(x["edits"] or 0))[:_MAX_ARQUIVOS]:
        estado = "sumiu do disco" if i.get("sumiu") else (
            "sem commit" if i.get("pendente") else "commitado")
        w("| `%s` | %d | %d | %s |" % (i["rel"], i["edits"], i["sessoes"], estado))
    if t.get("total", 0) > _MAX_ARQUIVOS:
        w("")
        w("_(%d arquivos no total)_" % t["total"])
    w("")


def _contexto(w, p):
    ctx = p.get("context_files") or []
    if not ctx:
        return
    w("## Documentos de contexto neste projeto")
    w("")
    w("Leia estes arquivos direto — eles não foram copiados para cá de propósito,")
    w("para não gastar contexto com o que você pode abrir:")
    w("")
    for c in ctx[:12]:
        tam = util.human_size(c.get("size") or 0)
        w("- `%s` (%s)" % (c.get("path") or c.get("name"), tam))
    w("")


def _sessoes(w, p):
    """As conversas que dizem alguma coisa -- e a contagem honesta do resto.

    Medido num projeto real: de 138 sessoes, **4** tem titulo aproveitavel. As
    outras 134 sao forks do Codex sem titulo, sem primeira mensagem e sem evento
    de prompt; listar as 12 mais recentes dava dez linhas de "sem titulo" em
    fila. Aqui as com texto vem primeiro, e as mudas viram uma linha so' --
    que ainda diz quantas sao e em que periodo, entao nada e' escondido.
    """
    ss = p.get("sessions") or []
    if not ss:
        return
    com, sem = [], []
    for s in ss:
        titulo = util.titulo_usavel(s.get("title")) or util.titulo_usavel(s.get("first_prompt"))
        (com if titulo else sem).append((titulo, s))

    w("## Últimas conversas")
    w("")
    for titulo, s in com[:_MAX_SESSOES]:
        w("- **%s** — %s%s, %s"
          % (util.clip(titulo, 90), s.get("ai") or "?",
             " " + s["model"] if s.get("model") else "",
             _quando(s.get("ended_at"), True)))
    if len(com) > _MAX_SESSOES:
        w("- _(e mais %d com título)_" % (len(com) - _MAX_SESSOES))
    if sem:
        datas = sorted(x.get("ended_at") or x.get("started_at") or 0 for _, x in sem)
        faixa = ""
        if datas[0] and datas[-1]:
            faixa = (" entre %s e %s" % (_quando(datas[0]), _quando(datas[-1]))
                     if datas[0] != datas[-1] else " em %s" % _quando(datas[0]))
        w("")
        w("_Outras %d conversas%s não têm título nem primeira mensagem guardada._"
          % (len(sem), faixa))
    w("")


def _fechamento(w, p):
    m = p.get("metas") or {}
    w("---")
    w("")
    w("### Como manter este dossiê útil")
    w("")
    if not m.get("tem"):
        w("1. Crie o `ROADMAP.md` no formato acima e marque as caixas conforme avançar.")
        w("2. O ProjectCommit lê esse arquivo sozinho e passa a mostrar a porcentagem.")
    else:
        w("1. Marque as caixas do `%s` conforme avançar — o painel lê de volta."
          % (", ".join(m.get("arquivos") or []) or "ROADMAP.md"))
    w("%d. Registre decisões no `CLAUDE.md` ou `AGENTS.md` do projeto."
      % (3 if not m.get("tem") else 2))


def salvar(projeto):
    """Grava o dossie em %LOCALAPPDATA% e devolve caminho + texto.

    Mesmo destino de transcript.export_markdown(): fora do projeto, entao nao
    passa pelo Modo Seguro.
    """
    texto = montar(projeto)
    destino = os.path.join(settings.data_dir(), "exports")
    os.makedirs(destino, exist_ok=True)
    seguro = "".join(
        c if (c.isalnum() or c in " -_") else "_" for c in (projeto.get("name") or "projeto")
    ).strip()[:60] or "projeto"
    caminho = os.path.join(
        destino, "%s - dossie %s.md" % (seguro, time.strftime("%Y-%m-%d_%H%M%S"))
    )
    with open(caminho, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(texto)
    return {"path": caminho, "texto": texto, "bytes": len(texto.encode("utf-8"))}
