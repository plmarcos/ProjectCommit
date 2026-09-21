"""Leitura do estado do git.

IMPORTANTE: todo comando leva `-c safe.directory=*`. Os projetos deste disco foram
criados por outra instalacao do Windows (o PC foi formatado em 2026-07-29) e o dono
NTFS tem SID diferente do usuario atual; sem isso o git responde
"fatal: detected dubious ownership in repository" e nao roda nada.

Este modulo NAO escreve. As acoes de escrita ficam em actions.py, atras do Modo Seguro.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import util

_BASE = ["git", "-c", "safe.directory=*", "-c", "core.quotepath=false"]

_NO_WINDOW = 0
if os.name == "nt":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def run(args, cwd=None, timeout=25):
    """Executa git e devolve (ok, stdout, stderr). Nunca levanta excecao."""
    try:
        proc = subprocess.run(
            _BASE + list(args),
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", str(exc)
    # rstrip apenas, NUNCA strip(): em `status --porcelain` a coluna 1 pode ser
    # um espaco significativo (" M arquivo" = alterado so' na arvore de trabalho).
    # Um strip() comia esse espaco da PRIMEIRA linha e deslocava o nome do
    # arquivo em um caractere -- "src/x" virava "rc/x".
    out = proc.stdout.decode("utf-8", "replace").rstrip("\r\n \t")
    err = proc.stderr.decode("utf-8", "replace").strip()
    return proc.returncode == 0, out, err


_GIT_OK = None


def git_available(force=False):
    """O executavel do git existe? Resultado em cache.

    O .exe empacotado nao carrega git dentro; ele depende do git do sistema.
    Sem esta checagem, rodar num ambiente sem git fazia CADA repositorio ser
    regravado com "[WinError 2]" -- ou seja, uma varredura destruia o estado bom
    que ja' estava no indice.
    """
    global _GIT_OK
    if _GIT_OK is None or force:
        if shutil.which("git") is None:
            _GIT_OK = False
        else:
            _GIT_OK = run(["--version"], timeout=8)[0]
    return _GIT_OK


def has_repo(path):
    return bool(path) and os.path.isdir(os.path.join(path, ".git"))


def _head_name(path):
    """Nome do branch quando ainda nao ha' commit (HEAD aponta para ref inexistente)."""
    ok, out, _ = run(["symbolic-ref", "--short", "HEAD"], cwd=path)
    return (out.strip() or None) if ok else None


def probe(path):
    """Retrato do repositorio para o card do projeto."""
    info = {
        "has_git": False, "branch": None, "commit_count": None, "dirty_count": None,
        "ahead": None, "behind": None, "remote": None,
        "last_ts": None, "last_msg": None, "last_author": None, "error": None,
    }
    if not has_repo(path):
        return info
    info["has_git"] = True
    if not git_available():
        info["error"] = "git nao encontrado no PATH"
        return info

    ok, out, err = run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if not ok:
        low = err.lower()
        if "not a git repository" in low:
            # Existe uma pasta .git, mas ela nao e' um repositorio (restos de copia).
            info["has_git"] = False
        elif "unknown revision" in low or "ambiguous argument" in low:
            # Repositorio recem-criado, ainda sem nenhum commit.
            info["branch"] = _head_name(path)
            info["commit_count"] = 0
            info["dirty_count"] = len(dirty_files(path))
            return info
        else:
            info["error"] = util.clip(err, 300)
        return info
    info["branch"] = out.strip() or None

    ok, out, _ = run(["rev-list", "--count", "HEAD"], cwd=path)
    if ok and out.strip().isdigit():
        info["commit_count"] = int(out.strip())

    ok, out, _ = run(["status", "--porcelain"], cwd=path)
    if ok:
        info["dirty_count"] = len([l for l in out.splitlines() if l.strip()])

    ok, out, _ = run(["log", "-1", "--format=%at%x1f%s%x1f%an"], cwd=path)
    if ok and out:
        parts = out.split("\x1f")
        info["last_ts"] = util.to_epoch(parts[0]) if parts else None
        info["last_msg"] = util.clip(parts[1], 200) if len(parts) > 1 else None
        info["last_author"] = parts[2] if len(parts) > 2 else None

    ok, out, _ = run(["remote", "get-url", "origin"], cwd=path)
    info["remote"] = (out.strip() or None) if ok else None

    ok, out, _ = run(["rev-list", "--left-right", "--count", "@{upstream}...HEAD"], cwd=path)
    if ok and out:
        bits = out.split()
        if len(bits) == 2 and all(b.isdigit() for b in bits):
            info["behind"], info["ahead"] = int(bits[0]), int(bits[1])

    return info


def recent_commits(path, limit=30, since=None):
    """Commits do HEAD. `since` (epoch) corta o log para a leitura incremental:
    depois da primeira varredura quase nunca ha' commit novo, e pedir 500 toda
    vez custaria segundos em 38 repositorios."""
    args = ["log", "-n", str(limit), "--format=%H%x1f%at%x1f%an%x1f%s"]
    if since:
        args.append("--since=@%d" % int(since))
    ok, out, _ = run(args, cwd=path)
    if not ok or not out:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        commits.append(
            {
                "sha": parts[0], "short": parts[0][:8],
                "ts": util.to_epoch(parts[1]),
                "author": parts[2], "message": parts[3],
            }
        )
    return commits


def dirty_files(path, limit=500):
    ok, out, _ = run(["status", "--porcelain=v1"], cwd=path)
    if not ok:
        return []
    files = []
    for line in out.splitlines()[:limit]:
        if len(line) < 4:
            continue
        files.append({"status": line[:2].strip() or "?", "path": line[3:].strip().strip('"')})
    return files


def diff_stat(path):
    ok, out, _ = run(["diff", "--stat", "HEAD"], cwd=path)
    return out.strip() if ok else ""


def refresh(con, project_id, path):
    """Le o estado e grava em git_state."""
    info = probe(path)
    con.execute(
        "INSERT INTO git_state(project_id, has_git, branch, commit_count, dirty_count, ahead, "
        "behind, remote, last_ts, last_msg, last_author, error, checked_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project_id) DO UPDATE SET has_git=excluded.has_git, branch=excluded.branch, "
        "commit_count=excluded.commit_count, dirty_count=excluded.dirty_count, ahead=excluded.ahead, "
        "behind=excluded.behind, remote=excluded.remote, last_ts=excluded.last_ts, "
        "last_msg=excluded.last_msg, last_author=excluded.last_author, error=excluded.error, "
        "checked_at=excluded.checked_at",
        (
            project_id, int(info["has_git"]), info["branch"], info["commit_count"],
            info["dirty_count"], info["ahead"], info["behind"], info["remote"],
            info["last_ts"], info["last_msg"], info["last_author"], info["error"], util.now(),
        ),
    )
    return info
