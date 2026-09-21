"""Acoes que saem da leitura: git, snapshots e abrir o projeto em outro programa.

REGRA DO MODO SEGURO
--------------------
O Modo Seguro protege as PASTAS DE PROJETO do usuario. Ele bloqueia o que escreve
la' dentro -- `git init`, `git commit`, `.gitignore` -- e e' verificado aqui, no
servidor, nao apenas na interface (a interface so' desabilita o botao).

Fora do bloqueio, porque nao tocam no projeto:
  * snapshot .zip  -> grava em %LOCALAPPDATA%\\ProjectCommit\\snapshots
  * abrir no editor -> so' dispara outro programa

As pastas de dados das IAs (.claude, .codex, .gemini) nunca sao escritas por
nenhuma funcao deste modulo.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import zipfile

from . import gitops, settings, util

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

# Nunca entram num snapshot nem contam no preflight.
EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".expo", ".gradle", ".pytest_cache", "target", "bin", "obj",
}

GITIGNORE_PADRAO = """# gerado pelo ProjectCommit
node_modules/
__pycache__/
*.py[cod]
.venv/
venv/
dist/
build/
.next/
.expo/
.gradle/
*.log
.env
.DS_Store
Thumbs.db
"""


class SafeModeError(RuntimeError):
    """Levantada quando o Modo Seguro barra uma escrita no projeto."""


class ActionError(RuntimeError):
    """Falha previsivel de uma acao, com mensagem legivel para a interface."""


def guard():
    if settings.load().get("safe_mode", True):
        raise SafeModeError(
            "Modo Seguro ligado: o programa nao escreve nos seus projetos. "
            "Desligue em Configuracoes para liberar."
        )


# ---- inspecao previa -------------------------------------------------------

def preflight(path, limit=40000):
    """Mede o projeto antes de um init/commit/snapshot.

    Existe porque ha' projeto de 18 mil arquivos e 20 GB nesta maquina: um
    `git add .` cego ali trava a maquina e polui o repositorio. A contagem para
    no limite para nao virar ela propria uma travada.
    """
    files = dirs = 0
    size = 0
    truncated = False
    for dirpath, subdirs, names in os.walk(path):
        subdirs[:] = [d for d in subdirs if d not in EXCLUDE_DIRS]
        dirs += len(subdirs)
        for name in names:
            files += 1
            st = util.stat_or_none(os.path.join(dirpath, name))
            if st:
                size += st.st_size
            if files >= limit:
                truncated = True
                break
        if truncated:
            break
    return {
        "files": files, "dirs": dirs, "size": size, "truncated": truncated,
        "has_gitignore": os.path.isfile(os.path.join(path, ".gitignore")),
        "has_git": gitops.has_repo(path),
        "pesado": truncated or size > 2 * 1024 ** 3 or files > 5000,
    }


# ---- git -------------------------------------------------------------------

def git_init(path, write_gitignore=True, first_commit=True, message=None):
    guard()
    if not os.path.isdir(path):
        raise ActionError("pasta nao encontrada: %s" % path)
    if gitops.has_repo(path):
        raise ActionError("este projeto ja' tem um repositorio git")

    ok, _out, err = gitops.run(["init"], cwd=path)
    if not ok:
        raise ActionError("git init falhou: %s" % err)

    criou_ignore = False
    ignore_path = os.path.join(path, ".gitignore")
    if write_gitignore and not os.path.isfile(ignore_path):
        with open(ignore_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(GITIGNORE_PADRAO)
        criou_ignore = True

    result = {"initialized": True, "gitignore": criou_ignore, "committed": False}
    if first_commit:
        result.update(git_commit(path, message or "Primeiro commit (ProjectCommit)", _skip_guard=True))
    return result


def git_commit(path, message=None, files=None, _skip_guard=False):
    if not _skip_guard:
        guard()
    if not gitops.has_repo(path):
        raise ActionError("este projeto nao tem repositorio git")

    if files:
        ok, _out, err = gitops.run(["add", "--"] + list(files), cwd=path)
    else:
        ok, _out, err = gitops.run(["add", "-A"], cwd=path)
    if not ok:
        raise ActionError("git add falhou: %s" % err)

    staged = gitops.run(["diff", "--cached", "--name-only"], cwd=path)[1]
    if not staged.strip():
        raise ActionError("nada para commitar")

    msg = message or suggest_message(path)
    ok, out, err = gitops.run(
        ["-c", "user.name=ProjectCommit", "-c", "user.email=projectcommit@local",
         "commit", "-m", msg],
        cwd=path,
    )
    if not ok:
        raise ActionError("git commit falhou: %s" % (err or out))
    sha = gitops.run(["rev-parse", "--short", "HEAD"], cwd=path)[1]
    return {"committed": True, "message": msg, "sha": sha,
            "files": len(staged.strip().splitlines())}


def suggest_message(path):
    """Mensagem sugerida, descritiva em vez de generica.

    Olha o INDICE quando ha' algo staged; senao olha a arvore de trabalho. A
    segunda metade importa: a interface pede a sugestao ANTES de commitar, e
    nesse instante o `git add` ainda nao rodou -- ler so' `--cached` devolvia
    sempre a frase generica.
    """
    status = [l for l in gitops.run(["status", "--porcelain=v1"], cwd=path)[1].splitlines() if l]
    if not status:
        return "Atualiza o projeto"

    novos = alterados = apagados = 0
    names = []
    for line in status:
        code, _, rest = line[:2], line[2], line[3:]
        names.append(rest.strip().strip('"').split(" -> ")[-1])
        letters = code.replace(" ", "")
        if "?" in code or "A" in letters:
            novos += 1
        elif "D" in letters:
            apagados += 1
        else:
            alterados += 1

    partes = []
    if novos:
        partes.append("%d %s" % (novos, "arquivo novo" if novos == 1 else "arquivos novos"))
    if alterados:
        partes.append("%d %s" % (alterados, "alterado" if alterados == 1 else "alterados"))
    if apagados:
        partes.append("%d %s" % (apagados, "removido" if apagados == 1 else "removidos"))

    # Pasta de topo mais afetada, para dar contexto ao resumo.
    topos = {}
    for n in names:
        topo = n.split("/")[0] if "/" in n else "raiz"
        topos[topo] = topos.get(topo, 0) + 1
    onde = max(topos.items(), key=lambda kv: kv[1])[0] if topos else None

    resumo = ", ".join(partes) or "%d arquivos" % len(names)
    if onde and onde != "raiz" and len(topos) <= 3:
        return "Atualiza %s em %s" % (resumo, onde)
    return "Atualiza %s" % resumo


# ---- snapshots -------------------------------------------------------------

def snapshot(path, name=None, label=None, max_bytes=3 * 1024 ** 3, apenas=None):
    """Copia versionada em .zip, fora do projeto.

    Nao passa pelo Modo Seguro porque grava em %LOCALAPPDATA%, nunca no projeto.

    `apenas` e' uma lista de caminhos relativos: salva SO' eles. Existe porque
    boa parte dos projetos sem git nao e' codigo -- sao instalacoes de jogo e
    pastas de midia onde a IA editou um punhado de arquivos de traducao ou
    configuracao. Medido: uma instalacao de jogo de 37 GB com 1 arquivo tocado;
    uma pasta de traducao de 9,7 GB com 22. Zipar a pasta inteira e' a ferramenta
    errada, e o teto de 3 GB so' faria a acao falhar depois de varrer tudo.
    """
    if not os.path.isdir(path):
        raise ActionError("pasta nao encontrada: %s" % path)

    if apenas:
        return _snapshot_arquivos(path, apenas, name, label)

    info = preflight(path)
    if info["size"] > max_bytes:
        raise ActionError(
            "projeto grande demais para snapshot (%s). Use git aqui."
            % util.human_size(info["size"])
        )

    base = name or util.basename(path) or "projeto"
    safe = "".join(c if (c.isalnum() or c in " -_") else "_" for c in base).strip() or "projeto"
    destino = os.path.join(settings.snapshots_dir(), safe)
    os.makedirs(destino, exist_ok=True)

    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    sufixo = ("-" + "".join(c if (c.isalnum() or c in " -_") else "_" for c in label).strip()) if label else ""
    zip_path = os.path.join(destino, "%s%s.zip" % (stamp, sufixo))

    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, subdirs, names in os.walk(path):
            subdirs[:] = [d for d in subdirs if d not in EXCLUDE_DIRS]
            for fname in names:
                full = os.path.join(dirpath, fname)
                try:
                    zf.write(full, os.path.relpath(full, path))
                    total += 1
                except OSError:
                    continue  # arquivo em uso ou sem permissao: segue o baile

    st = util.stat_or_none(zip_path)
    return {
        "path": zip_path, "files": total,
        "size": st.st_size if st else 0,
        "size_human": util.human_size(st.st_size if st else 0),
    }


def _destino_zip(path, name, label, sufixo_fixo=""):
    base = name or util.basename(path) or "projeto"
    safe = "".join(c if (c.isalnum() or c in " -_") else "_" for c in base).strip() or "projeto"
    destino = os.path.join(settings.snapshots_dir(), safe)
    os.makedirs(destino, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    sufixo = ("-" + "".join(c if (c.isalnum() or c in " -_") else "_" for c in label).strip()) if label else ""
    return os.path.join(destino, "%s%s%s.zip" % (stamp, sufixo_fixo, sufixo))


def _snapshot_arquivos(path, relativos, name=None, label=None):
    """Zipa apenas os caminhos relativos pedidos.

    Guarda contra caminho escapando do projeto -- a lista vem do indice, mas
    nada custa conferir antes de abrir arquivo por ela.
    """
    zip_path = _destino_zip(path, name, label, "-ia")
    raiz = os.path.abspath(path)
    total = faltando = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel in relativos:
            full = os.path.abspath(os.path.join(raiz, str(rel).replace("/", os.sep)))
            if not full.startswith(raiz + os.sep):
                continue
            if not os.path.isfile(full):
                faltando += 1
                continue
            try:
                zf.write(full, os.path.relpath(full, raiz))
                total += 1
            except OSError:
                faltando += 1
    st = util.stat_or_none(zip_path)
    if not total:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise ActionError("nenhum dos %d arquivos existe mais no disco" % len(relativos))
    return {
        "path": zip_path, "files": total, "faltando": faltando, "parcial": True,
        "size": st.st_size if st else 0,
        "size_human": util.human_size(st.st_size if st else 0),
    }


def restore_snapshot(project_path, zip_path, backup_first=True):
    """Extrai um snapshot de volta por cima do projeto.

    Isto ESCREVE no projeto, entao passa pelo Modo Seguro. Antes de sobrescrever
    faz um snapshot do estado atual -- restaurar sem rede de seguranca seria
    trocar um arrependimento por outro.
    """
    guard()
    if not os.path.isdir(project_path):
        raise ActionError("pasta do projeto nao encontrada")
    if not os.path.isfile(zip_path) or not zip_path.lower().endswith(".zip"):
        raise ActionError("snapshot invalido")
    if not util.is_under(zip_path, settings.snapshots_dir()):
        # So' restauramos de dentro da nossa propria pasta de snapshots.
        raise ActionError("este arquivo nao e' um snapshot do ProjectCommit")

    antes = None
    if backup_first:
        antes = snapshot(project_path, util.basename(project_path), "antes-de-restaurar")

    extraidos = 0
    with zipfile.ZipFile(zip_path) as zf:
        for membro in zf.namelist():
            destino = os.path.normpath(os.path.join(project_path, membro))
            # Zip Slip: um .zip malicioso poderia trazer "..\..\windows\x".
            if not util.is_under(destino, project_path):
                continue
            if membro.endswith("/"):
                continue
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            with zf.open(membro) as origem, open(destino, "wb") as saida:
                shutil.copyfileobj(origem, saida)
            extraidos += 1

    return {"restored": True, "files": extraidos,
            "backup": antes["path"] if antes else None}


def list_snapshots(path):
    base = util.basename(path) or ""
    safe = "".join(c if (c.isalnum() or c in " -_") else "_" for c in base).strip()
    destino = os.path.join(settings.snapshots_dir(), safe)
    if not os.path.isdir(destino):
        return []
    out = []
    for name in sorted(os.listdir(destino), reverse=True):
        if not name.endswith(".zip"):
            continue
        full = os.path.join(destino, name)
        st = util.stat_or_none(full)
        out.append({"name": name, "path": full,
                    "size": st.st_size if st else 0, "mtime": st.st_mtime if st else 0})
    return out


# ---- abrir em outro programa ----------------------------------------------

def _antigravity_exe():
    return os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "Programs", "Antigravity", "Antigravity.exe"
    )


def available_targets():
    """Quais destinos existem nesta maquina -- a interface so' mostra os que abrem."""
    alvos = {"explorer": os.name == "nt", "terminal": os.name == "nt"}
    alvos["vscode"] = bool(shutil.which("code") or shutil.which("code.cmd"))
    alvos["antigravity"] = os.path.isfile(_antigravity_exe())
    alvos["claude"] = bool(shutil.which("claude") or shutil.which("claude.cmd"))
    alvos["codex"] = bool(shutil.which("codex") or shutil.which("codex.cmd"))
    return alvos


def open_in(target, path):
    """Abre o projeto em outro programa. Nao escreve nada -- fora do Modo Seguro."""
    if not os.path.isdir(path):
        raise ActionError("pasta nao encontrada: %s" % path)
    alvos = available_targets()
    if not alvos.get(target):
        raise ActionError("'%s' nao esta' disponivel nesta maquina" % target)

    try:
        if target == "explorer":
            subprocess.Popen(["explorer", os.path.normpath(path)])
        elif target == "vscode":
            exe = shutil.which("code") or shutil.which("code.cmd")
            subprocess.Popen([exe, path], creationflags=_NO_WINDOW, shell=False)
        elif target == "antigravity":
            subprocess.Popen([_antigravity_exe(), path])
        elif target in ("claude", "codex", "terminal"):
            # Abre um console novo na pasta; para claude/codex ja' com a CLI rodando.
            cmd = {"claude": "claude", "codex": "codex", "terminal": ""}[target]
            linha = 'start "" cmd /K "cd /d "%s"%s"' % (path, (" && " + cmd) if cmd else "")
            subprocess.Popen(linha, shell=True)
        else:
            raise ActionError("destino desconhecido: %s" % target)
    except OSError as exc:
        raise ActionError("nao consegui abrir: %s" % exc)
    return {"opened": target, "path": path}
