"""Configuracao persistida em %LOCALAPPDATA%\\ProjectCommit\\settings.json."""
from __future__ import annotations

import json
import os

from . import util

APP_NAME = "ProjectCommit"

# Nasce VAZIO, de proposito. Cada maquina guarda os projetos num lugar
# diferente, e adivinhar caminho e' pior que perguntar: a tela de Configuracoes
# pede as pastas na primeira execucao.
#
# O programa nao fica inutil sem isto. As raizes servem para varrer pastas
# INTEIRAS em busca de projetos; os projetos em que as IAs realmente
# trabalharam continuam aparecendo sozinhos, descobertos pelo `cwd` gravado
# dentro de cada conversa (ver `indexer.pode_adotar`).
DEFAULT_ROOTS = []

# Pastas que nunca contam como projeto.
DEFAULT_IGNORE = [
    "node_modules",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".venv",
    "venv",
]

DEFAULTS = {
    "roots": DEFAULT_ROOTS,
    "ignore_names": DEFAULT_IGNORE,
    "safe_mode": True,          # ligado: o app so' le, nunca escreve nos projetos
    "theme": "dark",
    "live_window_minutes": 30,  # janela do selo AO VIVO
    "adopt_discovered": True,   # criar projeto para cwd fora das raizes (so' leitura)
    "language": "pt-BR",
    # Precos por MILHAO de tokens, em USD. Chega vazio de proposito: preco
    # inventado vira numero de dinheiro confiantemente errado. A interface
    # semeia a tabela com os modelos que existem no indice, zerados.
    "prices": {},
    "currency": "USD",
}


def data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def index_path():
    return os.path.join(data_dir(), "index.db")


def snapshots_dir():
    d = os.path.join(data_dir(), "snapshots")
    os.makedirs(d, exist_ok=True)
    return d


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    cfg["roots"] = [p for p in (util.clean_path(r) for r in cfg.get("roots") or []) if p]
    return cfg


def save(cfg):
    current = load()
    current.update(cfg or {})
    tmp = settings_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, settings_path())
    return current


# ---- Fontes das IAs -------------------------------------------------------

def home():
    return os.path.expanduser("~")


def claude_projects_dir():
    return os.path.join(home(), ".claude", "projects")


def codex_dir():
    return os.path.join(home(), ".codex")


def codex_state_db():
    """O nome tem o numero da migration; pegar o maior state_*.sqlite existente."""
    d = codex_dir()
    best, best_n = None, -1
    try:
        for name in os.listdir(d):
            if name.startswith("state_") and name.endswith(".sqlite"):
                try:
                    n = int(name[len("state_") : -len(".sqlite")])
                except ValueError:
                    n = 0
                if n > best_n:
                    best, best_n = os.path.join(d, name), n
    except OSError:
        pass
    return best


def antigravity_dir():
    return os.path.join(home(), ".gemini", "antigravity")
