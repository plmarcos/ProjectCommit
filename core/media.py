"""Imagens que vivem DENTRO do JSONL, não como arquivo no disco.

O que voce cola numa conversa nao vira arquivo: o Claude grava o bloco
`{"type":"image","source":{"type":"base64","media_type":...,"data":...}}` e o
Codex grava `{"type":"input_image","image_url":"data:image/png;base64,..."}`,
os dois embutidos na linha da mensagem. Por isso a galeria so' mostrava as
imagens de SAIDA (que sao arquivos de verdade em generated_images/) e nenhuma
das de entrada.

A tabela `inline_media` guarda apenas uma REFERENCIA -- arquivo, deslocamento da
linha e indice do bloco. Os bytes continuam onde estao; o endpoint faz `seek`,
decodifica o base64 na hora e serve. Nada e' duplicado no disco, o que importa
quando ha' 2,4 GB de historico.
"""
from __future__ import annotations

import base64
import binascii
import json
import re

from . import util

# Prefiltros por bytes, para nem parsear a linha quando nao ha' imagem.
MARCA_CLAUDE = b'"image"'
MARCA_CODEX = b'"input_image"'

_DATA_URL = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)

#: Tamanho maximo de uma imagem servida. Acima disso e' quase certo lixo.
MAX_BYTES = 24 * 1024 * 1024


def blocos_claude(rec):
    """[(indice, media_type, tamanho_estimado, papel)] dos blocos de imagem."""
    if rec.get("type") not in ("user", "assistant"):
        return []
    message = rec.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    out = []
    for i, bloco in enumerate(content):
        if not isinstance(bloco, dict) or bloco.get("type") != "image":
            continue
        src = bloco.get("source") or {}
        dados = src.get("data") or ""
        out.append((i, src.get("media_type") or "image/png", _bytes_de_base64(len(dados)),
                    rec.get("type")))
    return out


def blocos_codex(rec):
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return []
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    out = []
    for i, bloco in enumerate(content):
        if not isinstance(bloco, dict) or bloco.get("type") != "input_image":
            continue
        m = _DATA_URL.match(str(bloco.get("image_url") or ""))
        if not m:
            continue
        out.append((i, m.group(1), _bytes_de_base64(len(m.group(2))), payload.get("role")))
    return out


def _bytes_de_base64(n_chars):
    """Base64 gasta 4 caracteres a cada 3 bytes."""
    return int(n_chars * 3 / 4)


def registrar(con, session_id, project_id, ai, source_path, offset, length, ts, blocos):
    """Grava as referencias. UNIQUE torna a reindexacao idempotente."""
    if not blocos:
        return 0
    origem = {"user": "entrada", "assistant": "saida"}
    con.executemany(
        "INSERT OR IGNORE INTO inline_media"
        "(session_id, project_id, ai, origem, offset, length, bloco, media_type, bytes, ts, source_path) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (session_id, project_id, ai, origem.get(papel, "entrada"),
             offset, length, i, tipo, tam, ts, source_path)
            for (i, tipo, tam, papel) in blocos
        ],
    )
    return len(blocos)


def ler(row):
    """Devolve (media_type, bytes) de uma linha de inline_media.

    Le so' a linha apontada -- `seek` + `read(length)` -- e nao o arquivo, que
    pode ter meio giga.
    """
    caminho = row["source_path"]
    st = util.stat_or_none(caminho)
    if st is None or row["offset"] + row["length"] > st.st_size:
        return None, None
    try:
        with open(caminho, "rb") as fh:
            fh.seek(row["offset"])
            raw = fh.read(row["length"])
        rec = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None, None

    bruto = _dados_do_bloco(rec, row["bloco"], row["ai"])
    if not bruto:
        return None, None
    try:
        dados = base64.b64decode(bruto, validate=False)
    except (binascii.Error, ValueError):
        return None, None
    if len(dados) > MAX_BYTES:
        return None, None
    return (row["media_type"] or "image/png"), dados


def _dados_do_bloco(rec, indice, ai):
    if ai == "codex":
        content = ((rec.get("payload") or {}).get("content")) or []
        if indice >= len(content):
            return None
        m = _DATA_URL.match(str((content[indice] or {}).get("image_url") or ""))
        return m.group(2) if m else None

    content = ((rec.get("message") or {}).get("content")) or []
    if indice >= len(content):
        return None
    return ((content[indice] or {}).get("source") or {}).get("data")
