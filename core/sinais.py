"""Marcadores deixados no codigo: TODO, FIXME, BUG, HACK.

Isto NAO e' "o projeto tem N bugs" -- e' "ha' N marcadores que alguem deixou no
codigo em que a IA mexe". A interface diz isso com todas as letras, porque a
diferenca importa.

O recorte veio de medicao, nao de gosto. Num projeto de 39 mil arquivos:

    arvore inteira      33.400 arquivos   116 s   3.984 marcadores
    so' o que a IA tocou   586 arquivos   3,4 s      47 marcadores

Varrer tudo e' lento E ruidoso: os 3.984 sao quase todos de dependencia de
terceiro, que voce nunca vai consertar. Por isso a fonte e' `touched_files`,
nunca `os.walk` do projeto.

Os 47, porem, tambem eram falsos: **28 deles eram a palavra `BUGATTI`** -- um
carro -- num projeto sobre o jogo de corrida em que ele aparece. Com o
regex corrigido aquele projeto tem **zero** marcadores, e a varredura inteira
(1.320 arquivos, todos os projetos, 0,6 s) encontra **cinco**. Cinco achados de
verdade valem mais que 3.984 de mentira, mas quem for mexer aqui precisa saber
que o numero e' pequeno DE PROPOSITO -- nao esta' quebrado.
"""
from __future__ import annotations

import os
import re

from . import db, util

#: Exige DOIS-PONTOS (ou parenteses) logo depois do marcador. Nao e' capricho: em
#: portugues "todo" e' palavra comum, e sem essa exigencia a lista enchia de
#:
#:     "Gente da faixa: TODO MUNDO da loja"      "TODO botao desabilitado"
#:     "entao TODO usuario"                       "troca TODO o className"
#:
#: -- sete dos catorze sinais proprios da primeira medicao eram isso. O mesmo
#: corte mata a auto-referencia: a linha deste arquivo que LISTA os marcadores
#: ("BUG, FIXME, TODO, HACK") tambem aparecia como sinal.
#: A convencao real sempre escreve `TODO:` ou `BUG(alguem)`.
#:
#: O caractere ANTES do marcador tambem importa, e isso so' apareceu na medicao:
#: dos sete achados da varredura completa, DOIS eram este proprio programa
#: falando de marcadores -- `sinais.py` num comentario com crase (`TODO:`) e o
#: texto vazio da tela em `app.js` (`<code>TODO:</code>`). Quem cita um marcador
#: o envolve em crase, aspas ou tag; quem deixa um o escreve solto depois do `#`.
#: (E por isso esta linha nao pode trazer o exemplo escrito por extenso: ele
#: seria indistinguivel de um marcador de verdade -- e a varredura o pegou.)
#: Note que o proprio `BUGATTI` nao precisa disto: os dois-pontos ja' o barram --
#: e ele era 28 dos 47 "marcadores" que a primeira medicao viu no MC3.
_MARCA = re.compile(rb"(?:^|[^A-Za-z_`'\"\>])(BUG|FIXME|TODO|HACK|XXX|WIP)\s*[:(\[]")

#: Pasta de terceiro ou de saida gerada. O TODO do three.js copiado para dentro
#: do projeto nao e' problema seu: dos 43 sinais da primeira medicao, 29 vinham
#: de `vendor/FBXLoader.js` e de um dump do PCSX2.
_RUIDO = (
    os.sep + "vendor" + os.sep, os.sep + "node_modules" + os.sep,
    os.sep + "third_party" + os.sep, os.sep + "thirdparty" + os.sep,
    os.sep + "dist" + os.sep, os.sep + "build" + os.sep, os.sep + "out" + os.sep,
    os.sep + "externals" + os.sep, os.sep + "deps" + os.sep,
    os.sep + ".venv" + os.sep, os.sep + "site-packages" + os.sep,
    ".min.", ".bundle.",
)

#: Tres niveis. BUG e FIXME afirmam que algo esta' errado; TODO e WIP dizem que
#: falta fazer; HACK e XXX sao ressalva de quem escreveu.
NIVEL = {
    "BUG": "alto", "FIXME": "alto",
    "TODO": "medio", "WIP": "medio",
    "HACK": "baixo", "XXX": "baixo",
}
ORDEM = ("alto", "medio", "baixo")

#: So' arquivo de codigo e de texto. Binario nao tem comentario, e `.min.js`
#: costuma trazer o marcador da biblioteca original, nao o seu.
EXTENSOES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".java", ".kt", ".cs", ".go", ".rs", ".rb", ".php", ".swift",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm",
    ".css", ".scss", ".html", ".sh", ".ps1", ".bat", ".sql", ".lua",
    ".md", ".txt", ".yml", ".yaml", ".toml", ".ini",
}

#: Arquivo maior que isto nao e' codigo escrito a mao -- e' dado ou bundle.
_MAX_BYTES = 400 * 1024
_MAX_LINHA = 400


def relevante(caminho):
    if os.path.splitext(caminho)[1].lower() not in EXTENSOES:
        return False
    baixo = caminho.lower()
    return not any(r in baixo for r in _RUIDO)


def do_arquivo(caminho, limite=200):
    """[(nivel, marca, linha, texto)] de um arquivo. Silencioso se nao der para ler."""
    try:
        if os.path.getsize(caminho) > _MAX_BYTES:
            return []
        with open(caminho, "rb") as fh:
            bruto = fh.read(_MAX_BYTES)
    except OSError:
        return []

    saida = []
    for n, linha in enumerate(bruto.splitlines(), 1):
        if len(linha) > _MAX_LINHA:
            continue          # linha gigante e' bundle, nao comentario
        achado = _MARCA.search(linha)
        if not achado:
            continue
        marca = achado.group(1).decode("ascii")
        texto = linha.decode("utf-8", "replace").strip().lstrip("/#*- \t")
        saida.append((NIVEL[marca], marca, n, util.clip(texto, 240)))
        if len(saida) >= limite:
            break
    return saida


def varrer(con, report, progress=None):
    """Atualiza a tabela `sinais` para todo arquivo que a IA tocou.

    Incremental por mtime, como o resto do indice: arquivo que nao mudou desde a
    ultima passada nao e' relido.
    """
    alvos = [
        r["path"] for r in con.execute("SELECT DISTINCT path FROM touched_files")
        if relevante(r["path"])
    ]
    vistos = {
        r["path"]: r["mtime"]
        for r in con.execute("SELECT path, MAX(mtime) AS mtime FROM sinais GROUP BY path")
    }
    total = max(len(alvos), 1)
    novos = lidos = 0
    for i, caminho in enumerate(alvos):
        if progress and i % 50 == 0:
            progress(i, total, "Sinais: " + util.basename(caminho))
        st = util.stat_or_none(caminho)
        if st is None:
            # Arquivo sumiu: o sinal dele tambem some, senao a lista vira fantasma.
            con.execute("DELETE FROM sinais WHERE path=?", (caminho,))
            continue
        anterior = vistos.get(caminho)
        if anterior is not None and abs(anterior - st.st_mtime) < 0.001:
            continue
        lidos += 1
        marcas = do_arquivo(caminho)
        # Apaga tudo do arquivo antes: um marcador que mudou de linha deixaria
        # duas entradas se dependesse so' da chave (path, linha).
        con.execute("DELETE FROM sinais WHERE path=?", (caminho,))
        if marcas:
            con.executemany(
                "INSERT OR REPLACE INTO sinais(path, mtime, nivel, marca, linha, texto) "
                "VALUES(?,?,?,?,?,?)",
                [(caminho, st.st_mtime, n, m, ln, t) for n, m, ln, t in marcas],
            )
            novos += len(marcas)
        else:
            # Sem marcador, mas precisa registrar que este mtime ja' foi visto --
            # senao o arquivo limpo e' relido em toda varredura.
            con.execute(
                "INSERT OR REPLACE INTO sinais(path, mtime, nivel, marca, linha, texto) "
                "VALUES(?,?,'limpo','',0,NULL)",
                (caminho, st.st_mtime),
            )
        if lidos % 200 == 0:
            con.commit()
    con.commit()
    report.bump("sinais_arquivos_lidos", lidos)
    report.bump("sinais", novos)
    if progress:
        progress(total, total, "Sinais: concluido")


def por_projeto(con, raiz, limite=200):
    """Sinais dentro de um projeto, agrupados por nivel."""
    prefixo = (util.clean_path(raiz) or "").rstrip(util.SEP) + util.SEP
    if len(prefixo) < 4:
        return {"tem": False, "total": 0, "por_nivel": {}, "itens": []}
    linhas = con.execute(
        "SELECT path, nivel, marca, linha, texto FROM sinais "
        "WHERE nivel <> 'limpo' AND INSTR(LOWER(path), LOWER(?)) = 1",
        (prefixo,),
    ).fetchall()

    por_nivel = {}
    itens = []
    for r in linhas:
        por_nivel[r["nivel"]] = por_nivel.get(r["nivel"], 0) + 1
        itens.append(
            {
                "path": r["path"],
                "rel": r["path"][len(prefixo):],
                "nivel": r["nivel"], "marca": r["marca"],
                "linha": r["linha"], "texto": r["texto"],
            }
        )
    itens.sort(key=lambda i: (ORDEM.index(i["nivel"]) if i["nivel"] in ORDEM else 9,
                              i["rel"].lower(), i["linha"]))
    return {
        "tem": bool(itens),
        "total": len(itens),
        "por_nivel": por_nivel,
        "itens": itens[:limite],
        "cortado": max(0, len(itens) - limite),
    }
