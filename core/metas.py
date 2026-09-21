"""Metas declaradas do projeto, lidas dos markdown que ele ja' tem.

O painel nao inventa meta e nao guarda meta: ele LE o que voce (ou a IA) escreveu
num `ROADMAP.md`, `TODO.md`, `task.md`. A fonte e' o arquivo, entao a lista fica
sempre em dia com o que a IA acabou de mexer, sem sincronizacao nenhuma.

Formato, tirado de um roadmap real (fases como titulo, caixas como itens):

    ## Fase 1 — Backend sem UI
    - [x] `backend/core.py` — conversao audio->RSM, provado.
    - [~] Objeto de estado: `core.Workspace` ja' centraliza os caminhos.
    - [ ] read/extract: `read_dave`/`read_hash` + `strtbl`.

O `[~]` NAO e' enfeite: ele existe naquele arquivo e vale meio ponto. Ignora-lo
mudaria a porcentagem, e uma porcentagem errada e' pior que nenhuma.

Uso pela linha de comando:
    python -m core.metas "C:\\caminho\\do\\projeto"
"""
from __future__ import annotations

import os
import re

from . import util

#: Onde procurar, em ordem de preferencia. Sao os nomes que a comunidade ja' usa
#: para plano -- nao inventamos um arquivo novo para voce ter que manter.
ARQUIVOS = (
    "ROADMAP.md", "TODO.md", "PLANO.md", "TAREFAS.md",
    "task.md", "TASKS.md", "HANDOFF.md", "RETOMAR-AQUI.md",
)

#: `- [x] texto`, com indentacao opcional. Os tres estados sao os que aparecem no
#: roadmap real; qualquer outro caractere dentro dos colchetes conta como aberto.
_CAIXA = re.compile(r"^(\s*)[-*+]\s+\[([ xX~\-])\]\s*(.+?)\s*$")
_TITULO = re.compile(r"^(#{1,4})\s+(.+?)\s*#*$")

#: Quanto cada estado vale no progresso. O parcial e' meio de proposito: dizer
#: que esta' pronto seria mentira, dizer que nao comecou tambem.
PESO = {"feita": 1.0, "parcial": 0.5, "aberta": 0.0}

_ESTADO = {"x": "feita", "X": "feita", "~": "parcial", "-": "parcial", " ": "aberta"}

#: Modelo copiavel oferecido a quem ainda nao declarou meta nenhuma.
EXEMPLO = """# Roadmap

## Fase 1 — o que já está de pé
- [x] Primeira coisa, já concluída
- [~] Segunda coisa, começada pela metade

## Fase 2 — o que falta
- [ ] Terceira coisa, ainda não feita
"""


def ler(raiz, extras=()):
    """Metas de um projeto. Devolve dicionario pronto para a interface.

    `extras` sao caminhos adicionais de markdown (os `brain/<id>/task.md` do
    Antigravity, que ja' estao indexados em `artifacts` e nao ficam na raiz).
    """
    fontes = []
    for nome in ARQUIVOS:
        caminho = os.path.join(raiz or "", nome)
        if os.path.isfile(caminho):
            fontes.append(caminho)
    for caminho in extras or ():
        if caminho not in fontes and os.path.isfile(caminho):
            fontes.append(caminho)

    grupos, arquivos = [], []
    for caminho in fontes:
        try:
            with open(caminho, encoding="utf-8", errors="replace") as fh:
                texto = fh.read(400_000)
        except OSError:
            continue
        achados = _do_texto(texto, util.basename(caminho))
        if achados:
            grupos.extend(achados)
            arquivos.append(util.basename(caminho))

    itens = [i for g in grupos for i in g["itens"]]
    total = len(itens)
    feitas = sum(1 for i in itens if i["estado"] == "feita")
    parciais = sum(1 for i in itens if i["estado"] == "parcial")
    pontos = sum(PESO[i["estado"]] for i in itens)
    return {
        "tem": total > 0,
        "arquivos": arquivos,
        "grupos": grupos,
        "total": total,
        "feitas": feitas,
        "parciais": parciais,
        "abertas": total - feitas - parciais,
        # Arredondado so' na exibicao; aqui vai o numero cru, conferivel.
        "percentual": round(pontos / total * 100) if total else 0,
        "exemplo": EXEMPLO,
    }


def _do_texto(texto, fonte):
    """Caixas agrupadas pelo titulo que vem acima delas."""
    grupos, atual = [], None
    titulo = None
    for n, linha in enumerate(texto.splitlines(), 1):
        cab = _TITULO.match(linha)
        if cab:
            titulo = cab.group(2).strip()
            atual = None          # o proximo item abre um grupo novo
            continue
        caixa = _CAIXA.match(linha)
        if not caixa:
            continue
        if atual is None:
            atual = {"titulo": titulo or fonte, "fonte": fonte, "itens": []}
            grupos.append(atual)
        atual["itens"].append(
            {
                "estado": _ESTADO.get(caixa.group(2), "aberta"),
                "texto": util.clip(_limpar(caixa.group(3)), 300),
                "nivel": len(caixa.group(1)) // 2,
                "linha": n,
                "fonte": fonte,
            }
        )
    return grupos


def _limpar(texto):
    """Tira o negrito e a crase do markdown -- a lista ja' tem formatacao propria."""
    texto = re.sub(r"\*\*(.+?)\*\*", r"\1", texto)
    texto = re.sub(r"`([^`]+)`", r"\1", texto)
    return texto.strip(" -—")


def main(argv=None):
    import sys

    alvos = list(argv or sys.argv[1:])
    if not alvos:
        print("uso: python -m core.metas <caminho do projeto> [...]")
        return 2
    for raiz in alvos:
        d = ler(raiz)
        print("\n=== %s" % raiz)
        if not d["tem"]:
            print("  nenhuma meta declarada (procurei: %s)" % ", ".join(ARQUIVOS[:4]))
            continue
        print("  %d metas em %s: %d feitas, %d parciais, %d abertas -> %d%%"
              % (d["total"], ", ".join(d["arquivos"]), d["feitas"],
                 d["parciais"], d["abertas"], d["percentual"]))
        for g in d["grupos"]:
            print("   [%s]" % g["titulo"][:70])
            for i in g["itens"][:40]:
                marca = {"feita": "x", "parcial": "~", "aberta": " "}[i["estado"]]
                print("     [%s] %s" % (marca, i["texto"][:76]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
