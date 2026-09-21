"""Deteccao do que esta' acontecendo agora.

Dois sinais, combinados de proposito:

  1. PROCESSO -- diz qual app de IA esta' aberto, mas nao diz em que projeto.
     claude.exe / codex.exe / Antigravity.exe aparecem no tasklist.
  2. MTIME DO ARQUIVO DE SESSAO -- este sim aponta o projeto. Enquanto uma IA
     trabalha ela grava continuamente no JSONL / rollout / .db daquela sessao.

O selo AO VIVO exige o sinal 2 (projeto). O sinal 1 so' enriquece o rotulo.
"""
from __future__ import annotations

import os
import subprocess

from . import util

# imagem do processo -> IA. Minusculo para comparar sem sustos.
PROCESS_MAP = {
    "claude.exe": "claude",
    "codex.exe": "codex",
    "antigravity.exe": "antigravity",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
_CACHE = {"at": 0.0, "value": {}}
_TTL = 4.0


def running_apps(force=False):
    """Quais apps de IA estao abertos. Resultado em cache por alguns segundos.

    Usa tasklist em vez de WMI/psutil: sem dependencia externa e ~20x mais rapido
    que uma consulta Win32_Process.
    """
    if not force and (util.now() - _CACHE["at"]) < _TTL:
        return _CACHE["value"]

    found = {name: False for name in set(PROCESS_MAP.values())}
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10, creationflags=_NO_WINDOW,
            )
            text = proc.stdout.decode("utf-8", "replace")
            for line in text.splitlines():
                if not line.startswith('"'):
                    continue
                image = line.split('","', 1)[0].lstrip('"').lower()
                ai = PROCESS_MAP.get(image)
                if ai:
                    found[ai] = True
        except (OSError, subprocess.TimeoutExpired):
            pass

    _CACHE["at"] = util.now()
    _CACHE["value"] = found
    return found


def live_sessions(con, window_minutes=30, lookback_days=14):
    """Sessoes cujo arquivo de origem foi escrito dentro da janela.

    Le o mtime real do disco em vez de confiar no ended_at indexado: o indice
    pode estar velho, o disco nunca esta'.
    """
    cutoff = util.now() - window_minutes * 60
    horizon = util.now() - lookback_days * 86400
    rows = con.execute(
        "SELECT s.id, s.ai, s.project_id, s.source_path, s.title, s.model, s.ended_at, "
        "       p.name AS project_name, p.path AS project_path "
        "FROM sessions s LEFT JOIN projects p ON p.id = s.project_id "
        "WHERE s.source_path IS NOT NULL AND COALESCE(s.ended_at, 0) > ? "
        "ORDER BY s.ended_at DESC",
        (horizon,),
    ).fetchall()

    out = []
    for row in rows:
        st = util.stat_or_none(row["source_path"])
        if st is None:
            continue
        seen = max(st.st_mtime, row["ended_at"] or 0)
        if seen < cutoff:
            continue
        out.append(
            {
                "session_id": row["id"], "ai": row["ai"], "project_id": row["project_id"],
                "project_name": row["project_name"], "project_path": row["project_path"],
                "title": row["title"], "model": row["model"],
                "last_seen": seen, "age_seconds": util.now() - seen,
            }
        )
    out.sort(key=lambda d: d["last_seen"], reverse=True)
    return out


def snapshot(con, window_minutes=30):
    """Pacote pronto para o topo do Radar."""
    apps = running_apps()
    sessions = live_sessions(con, window_minutes)
    by_project = {}
    for item in sessions:
        pid = item["project_id"]
        if pid is None:
            continue
        current = by_project.get(pid)
        if current is None or item["last_seen"] > current["last_seen"]:
            by_project[pid] = item
    return {
        "apps": apps,
        "sessions": sessions,
        "live_project_ids": sorted(by_project),
        "by_project": {str(k): v for k, v in by_project.items()},
        "window_minutes": window_minutes,
        "checked_at": util.now(),
    }
