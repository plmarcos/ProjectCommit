"""Quais ARQUIVOS cada IA editou -- extraidos das mesmas linhas ja' lidas.

O indice sabia em qual projeto cada IA trabalhou, mas nunca abria a caixa. O
dado sempre esteve la':

  Claude  message.content[] com {"type":"tool_use","name":"Edit",
          "input":{"file_path":"E:\\...\\scene.py"}}
  Codex   payload {"type":"custom_tool_call","input": '... *** Update File: E:\\...'}

Cruzado com `gitops.dirty_files()`, e' o que responde "a IA mexeu em 12 arquivos
aqui, 5 nunca foram commitados" -- a pergunta que da' nome ao programa.

Guardamos o caminho ABSOLUTO. O recorte "dentro do projeto" fica para a consulta
(`path LIKE <raiz>%`), porque a IA tambem edita coisas fora dele -- memoria,
configuracao, arquivo de outro projeto -- e isso nao deve poluir a lista.
"""
from __future__ import annotations

import os
import re

from . import util

#: Ferramentas do Claude que ESCREVEM. Read/Grep/Bash ficam de fora: interessa
#: o que mudou o disco, nao o que foi olhado.
ESCRITA_CLAUDE = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Update"}

#: Prefiltro por bytes para o rollout do Codex, antes de qualquer json.loads.
MARCA_CODEX = b"apply_patch"
MARCA_CODEX_ALT = b"Update File:"

#: Prefiltro por bytes para o JSONL do Claude.
MARCA_CLAUDE = b'"tool_use"'

# O `input` do Codex e' CODIGO JAVASCRIPT, e o patch vai dentro dele como string
# literal -- entao a quebra de linha ali sao os DOIS caracteres '\' e 'n', nao
# uma quebra de verdade. Um `(.+)` guloso engolia o patch inteiro e gravava
# "HANDOFF.md\n@@\n   response, then props\exterior\veh" como se fosse caminho.
# Daqui em diante o corte e' no primeiro terminador, seja qual for.
_PATCH_RE = re.compile(r"\*\*\* (Update|Add|Delete) File: (.+?)\s*(?:\\n|\\r|[\r\n\"]|$)")

#: Familias de arquivo. Seis, nao quarenta: a cor neste app e' identidade de IA, e
#: uma cor por extensao destruiria isso. O icone e' que distingue a familia.
FAMILIAS = {
    "codigo": (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
               ".java", ".kt", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".lua",
               ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".sh", ".ps1", ".bat",
               ".sql", ".r", ".pl"),
    "estilo":   (".css", ".scss", ".sass", ".less", ".html", ".htm", ".xaml"),
    "documento": (".md", ".txt", ".rst", ".pdf", ".doc", ".docx", ".rtf"),
    "dado":     (".json", ".csv", ".tsv", ".xml", ".db", ".sqlite", ".parquet", ".jsonl"),
    "config":   (".yml", ".yaml", ".toml", ".ini", ".cfg", ".env", ".conf", ".properties",
                 ".gitignore", ".editorconfig", ".spec"),
    "midia":    (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".mp3", ".wav",
                 ".mp4", ".mov", ".webm", ".ttf", ".otf", ".woff", ".woff2"),
}

_POR_EXT = {ext: fam for fam, exts in FAMILIAS.items() for ext in exts}

#: Nome sem extensao que ainda assim tem familia obvia.
_POR_NOME = {
    "dockerfile": "config", "makefile": "config", "procfile": "config",
    "license": "documento", "readme": "documento",
}


def familia(caminho):
    """A que familia um arquivo pertence. 'outro' quando nao da' para saber."""
    base = util.basename(caminho) or ""
    ext = os.path.splitext(base)[1].lower()
    if ext in _POR_EXT:
        return _POR_EXT[ext]
    if not ext and base.lower() in _POR_NOME:
        return _POR_NOME[base.lower()]
    if base.lower().startswith("."):
        return "config"          # .gitignore, .editorconfig, .env.local
    return "outro"

_ACAO = {"Update": "editou", "Add": "criou", "Delete": "apagou"}


def de_claude(rec):
    """[(caminho, ferramenta)] de um registro do Claude ja' parseado."""
    message = rec.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    saida = []
    for bloco in content:
        if not isinstance(bloco, dict) or bloco.get("type") != "tool_use":
            continue
        nome = bloco.get("name")
        if nome not in ESCRITA_CLAUDE:
            continue
        entrada = bloco.get("input")
        if not isinstance(entrada, dict):
            continue
        alvo = entrada.get("file_path") or entrada.get("notebook_path")
        if alvo:
            saida.append((util.clean_path(alvo), nome))
    return saida


def de_codex(rec):
    """[(caminho, acao)] de um registro do Codex ja' parseado.

    O Codex nao expoe o caminho num campo: ele manda o programa que aplica o
    patch, e o cabecalho `*** Update File:` dele carrega o caminho absoluto.
    """
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return []
    if payload.get("type") not in ("custom_tool_call", "function_call", "local_shell_call"):
        return []
    corpo = payload.get("input") or payload.get("arguments")
    if not isinstance(corpo, str) or "File:" not in corpo:
        return []
    saida = []
    for verbo, bruto in _PATCH_RE.findall(corpo):
        caminho = util.clean_path(_desescapar(bruto.strip().strip('"')))
        if caminho and ":" in caminho[:3]:
            saida.append((caminho, _ACAO.get(verbo, verbo.lower())))
    return saida


def _desescapar(bruto):
    r"""Colapsa a barra invertida dobrada pela string literal de JS.

    Metade dos patches chega como `C:\\Projetos\\...` (o Codex monta o patch
    dentro de uma string de JavaScript) e a outra metade como `C:\Projetos\...`
    (heredoc). Caminho do Windows nunca tem duas barras seguidas -- salvo o
    prefixo UNC `\\servidor`, que fica preservado.
    """
    prefixo = "\\\\" if bruto.startswith("\\\\") else ""
    return prefixo + bruto[len(prefixo):].replace("\\\\", "\\")


def acumular(baldes, itens, ts):
    """Junta [(caminho, ferramenta)] num dicionario caminho -> contagem e datas."""
    for caminho, ferramenta in itens:
        b = baldes.get(caminho)
        if b is None:
            baldes[caminho] = {"tool": ferramenta, "edits": 1, "first": ts, "last": ts}
            continue
        b["edits"] += 1
        b["tool"] = ferramenta
        if ts:
            b["first"] = min(b["first"] or ts, ts)
            b["last"] = max(b["last"] or ts, ts)


def gravar(con, session_id, project_id, ai, baldes, substituir=False):
    """Grava os baldes de uma sessao. Devolve quantos caminhos distintos entraram.

    `substituir=True` apaga a sessao antes -- e' o modo do backfill, que le o
    arquivo INTEIRO e por isso e' autoridade sobre o total. O modo padrao SOMA,
    porque a leitura corrente e' incremental: a cada varredura chega so' o
    pedaco que o arquivo cresceu.
    """
    if substituir:
        con.execute("DELETE FROM touched_files WHERE session_id=?", (session_id,))
    if not baldes:
        return 0
    con.executemany(
        "INSERT INTO touched_files(session_id, project_id, ai, path, tool, edits, first_ts, last_ts) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_id, path) DO UPDATE SET "
        "  edits = edits + excluded.edits, tool = excluded.tool, "
        "  project_id = COALESCE(excluded.project_id, project_id), "
        "  first_ts = MIN(COALESCE(first_ts, excluded.first_ts), COALESCE(excluded.first_ts, first_ts)), "
        "  last_ts  = MAX(COALESCE(last_ts,  excluded.last_ts),  COALESCE(excluded.last_ts,  last_ts))",
        [
            (session_id, project_id, ai, caminho, b["tool"], b["edits"], b["first"], b["last"])
            for caminho, b in baldes.items()
        ],
    )
    return len(baldes)
