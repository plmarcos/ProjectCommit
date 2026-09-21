"""Utilidades compartilhadas: normalizacao de caminho, tempo e leitura segura."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone

SEP = "\\"

# Prefixo de caminho longo do Windows que o Codex grava no sqlite: \\?\C:\...
_LONGPATH = re.compile(r"^\\\\\?\\(UNC\\)?")


def clean_path(raw):
    r"""Remove o prefixo \\?\, normaliza barras e tira a barra final."""
    if not raw:
        return None
    p = str(raw).strip()
    p = _LONGPATH.sub(lambda m: SEP + SEP if m.group(1) else "", p)
    p = p.replace("/", SEP)
    while len(p) > 3 and p.endswith(SEP):
        p = p[:-1]
    # "e:\x" -> "E:\x": a letra do drive vem em caixa baixa em varias fontes.
    if len(p) > 1 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p or None


def path_key(raw):
    """Chave canonica para comparar caminhos no Windows (case-insensitive)."""
    p = clean_path(raw)
    return p.lower() if p else None


def is_under(child, parent):
    c, p = path_key(child), path_key(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + SEP)


def basename(raw):
    p = clean_path(raw)
    return p.rsplit(SEP, 1)[-1] if p else None


def slug_to_path(slug):
    r"""Converte o slug do Claude Code de volta para caminho.

    'C--Users-eu-Projetos-Meu-App' -> 'C:\Users\eu\Projetos\Meu-App'

    O slug e' ambiguo (hifens do nome original viram separadores tambem), entao
    isto e' apenas um palpite de fallback -- o 'cwd' de dentro do JSONL e' a verdade.
    """
    if not slug or len(slug) < 3 or slug[1:3] != "--":
        return None
    return clean_path(slug[0] + ":" + SEP + slug[3:].replace("-", SEP))


def to_epoch(value):
    """Aceita ISO-8601, epoch em segundos ou em milissegundos."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # milissegundos
            v /= 1000.0
        return v if v > 0 else None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        try:
            return to_epoch(float(s))
        except ValueError:
            return None


def now():
    return time.time()


def stat_or_none(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def clip(text, limit=400):
    if not text:
        return None
    t = " ".join(str(text).split())
    return t if len(t) <= limit else t[: limit - 1] + "\u2026"


#: Titulo que NAO serve de rotulo. Espelha `LIXO_TITULO`/`SO_UUID` de `web/app.js`
#: -- o titulo de thread do Codex e' muitas vezes o proprio prompt de sistema que
#: a ferramenta injeta ("The following is the Codex agent history..."), e o
#: `native_id` cru e' um UUID que nao diz nada.
_LIXO_TITULO = re.compile(r"^(the following is|you are|<|system:|# )", re.I)
_SO_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def titulo_usavel(t):
    s = (t or "").strip()
    if not s or _SO_UUID.match(s) or _LIXO_TITULO.match(s):
        return None
    return s


def rotulo_conversa(s, corte=90):
    """Como uma conversa se chama. Gemeo de `rotuloConversa()` em `web/app.js`.

    Sem filtro, `title or first_prompt` traz o prompt de sistema do Codex para a
    tela -- e quando nao ha' nada legivel, o fallback precisa de HORA: so' com a
    data, varias conversas do mesmo dia ficam com texto identico.
    """
    bom = titulo_usavel(s.get("title")) or titulo_usavel(s.get("first_prompt"))
    if bom:
        return clip(bom, corte)
    ts = s.get("ended_at") or s.get("started_at")
    quando = time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else "sem data"
    return "sem título · " + quando
