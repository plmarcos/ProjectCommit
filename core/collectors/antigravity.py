"""Coletor do Antigravity (Google).

Fonte: %USERPROFILE%\\.gemini\\antigravity\\  -- NAO a pasta AppData\\Roaming\\Antigravity,
que e' so' cache do Electron.

  annotations\\<id>.pbtxt   texto puro: title:"..." last_user_view_time:{seconds:...}
  conversations\\<id>.db    sqlite com blobs PROTOBUF (schema nao publicado)
  brain\\<id>\\*.md          planos, walkthroughs e analises em markdown legivel
  brain\\<id>\\.user_uploaded\\, .system_generated\\   imagens

ATENCAO -- parte disto e' heuristica: o caminho do workspace nao existe em nenhum
campo legivel, entao ele e' extraido por varredura de strings dentro dos blobs
protobuf. Se o Google mudar o formato, esta funcao para de achar o projeto e a
conversa cai em "projeto desconhecido" -- ela nao deve derrubar a varredura.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections import Counter

from .. import agsteps, db, indexer, settings, transcript, util

AI = "antigravity"
KIND = "antigravity_db"

_TITLE_RE = re.compile(r'title:\s*"([^"]*)"')
_SECONDS_RE = re.compile(r"last_user_view_time:\s*\{\s*seconds:\s*(\d+)")
# Caminhos absolutos do Windows dentro dos blobs (com barra normal ou invertida).
_PATH_RE = re.compile(rb'[A-Za-z]:[\\/][^\x00-\x1f"\'<>|*?]{3,160}')
_MODEL_RE = re.compile(rb"(gemini[-a-z0-9.]{2,40})", re.I)

# Caminhos que nunca sao projeto do usuario.
_NOISE = ("\\.gemini\\", "\\appdata\\", "\\program files", "\\windows\\", "\\node_modules\\",
          "\\.vscode\\", "\\temp\\", "\\.git\\")

_BLOB_TABLES = ("trajectory_metadata_blob", "executor_metadata", "gen_metadata")


def collect(con, resolver, report, progress=None):
    roots = settings.load().get("roots") or []
    root = settings.antigravity_dir()
    convo_dir = os.path.join(root, "conversations")
    if not os.path.isdir(convo_dir):
        report.warn("antigravity: pasta de conversas nao encontrada")
        return

    files = sorted(f for f in os.listdir(convo_dir) if f.endswith(".db"))
    total = max(len(files), 1)
    for i, name in enumerate(files):
        native_id = name[:-3]
        if progress:
            progress(i, total, "Antigravity: " + native_id[:8])
        try:
            _collect_conversation(con, resolver, report, root, convo_dir, native_id, roots)
        except Exception as exc:
            report.warn("antigravity %s: %s" % (native_id[:8], exc))
        con.commit()
    if progress:
        progress(total, total, "Antigravity: concluido")


def _collect_conversation(con, resolver, report, root, convo_dir, native_id, roots=()):
    dbpath = os.path.join(convo_dir, native_id + ".db")
    st = util.stat_or_none(dbpath)
    if st is None:
        return
    sid = AI + ":" + native_id

    title, viewed_at = _read_annotation(root, native_id)
    prev = db.source_state(con, dbpath)
    unchanged = (
        prev is not None
        and prev["size"] == st.st_size
        and abs(prev["mtime"] - st.st_mtime) < 0.001
    )

    if unchanged:
        # Nada mudou no blob; ainda assim atualiza titulo/data vindos do pbtxt,
        # que e' um arquivo separado e barato de reler.
        indexer.merge_session(
            con, sid, AI, native_id, title=util.clip(title, 200), ended_at=viewed_at
        )
        report.bump("antigravity_skipped")
        _index_brain(con, resolver, report, root, native_id, sid, None)
        return

    lido = _ler_conversa(dbpath, report, roots)
    cwd = lido["cwd"]
    project_id = resolver.resolve(cwd) if cwd else None
    if cwd is None:
        report.bump("antigravity_sem_projeto")

    # O titulo do pbtxt tem precedencia porque e' o que a pessoa ve' no proprio
    # Antigravity -- mas em 9 das 12 conversas ele simplesmente nao existe, e
    # ate' agora elas apareciam sem nome. O cabecalho do .db cobre esse buraco.
    title = title or lido["titulo"]

    ended = max([t for t in (viewed_at, st.st_mtime, lido["fim"]) if t] or [None])
    started = min([t for t in (getattr(st, "st_ctime", None), lido["inicio"]) if t] or [None])
    indexer.merge_session(
        con, sid, AI, native_id,
        # absolute=True porque esta leitura ve' a conversa INTEIRA a cada vez --
        # nao ha' offset incremental num blob. Somar dobraria msg_count sempre
        # que o arquivo mudasse.
        absolute=True,
        project_id=project_id, cwd=cwd, title=util.clip(title, 200), model=lido["model"],
        source_path=dbpath, size_bytes=st.st_size,
        started_at=started, ended_at=ended,
        msg_count=lido["msgs"],
        first_prompt=util.clip(lido["primeiro"], 500),
    )
    if ended:
        indexer.add_event(
            con, "antigravity:session:" + native_id, AI, ended, "session",
            util.clip(title or native_id, 300), session_id=sid, project_id=project_id,
        )

    # Os prompts entram na linha do tempo e na busca como os das outras IAs.
    for idx, ts, texto in lido["prompts"]:
        if indexer.add_event(
            con, "antigravity:prompt:%s:%d" % (native_id, idx), AI, ts, "prompt",
            util.clip(texto, 2000), session_id=sid, project_id=project_id,
        ):
            report.bump("antigravity_prompts")

    for idx, texto in lido["respostas"]:
        db.fts_replace(con, "msg:%s:%d" % (sid, idx), texto[:transcript.LIMITE_BUSCA])
    if lido["respostas"]:
        report.bump("antigravity_respostas", len(lido["respostas"]))

    _index_brain(con, resolver, report, root, native_id, sid, project_id)
    db.set_source_state(
        con, dbpath, KIND, st.st_size, st.st_mtime, 0,
        note=("cwd=" + cwd) if cwd else "cwd desconhecido",
    )
    report.bump("antigravity_conversas")


def _read_annotation(root, native_id):
    path = os.path.join(root, "annotations", native_id + ".pbtxt")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None, None
    m = _TITLE_RE.search(text)
    s = _SECONDS_RE.search(text)
    return (m.group(1) if m else None), (float(s.group(1)) if s else None)


def _ler_conversa(dbpath, report, roots=()):
    """Le uma conversa inteira: fala, titulo, datas, modelo e diretorio.

    Duas fontes na mesma abertura do banco:

      * `steps`, decodificada por core/agsteps.py -- da' a conversa legivel, o
        titulo e, nas chamadas de ferramenta, o `Cwd` DECLARADO;
      * os blobs de metadados, varridos por regex de caminho -- e' a heuristica
        antiga, que continua valendo como rede quando nao ha' `Cwd` declarado.
    """
    vazio = {
        "cwd": None, "model": None, "titulo": None, "primeiro": None,
        "prompts": [], "respostas": [], "msgs": 0, "inicio": None, "fim": None,
    }
    try:
        ro = db.open_readonly(dbpath)
    except sqlite3.Error as exc:
        report.warn("antigravity: %s ilegivel (%s)" % (os.path.basename(dbpath), exc))
        return vazio

    candidates = Counter()
    declarados = Counter()
    model = None
    prompts, respostas, datas = [], [], []
    titulo = primeiro = None
    msgs = 0
    try:
        (titulo, primeiro), mensagens = agsteps.percorrer(ro)
        for idx, passo in mensagens:
            msgs += 1
            if passo["ts"]:
                datas.append(passo["ts"])
            if passo["papel"] == "user" and passo["texto"]:
                prompts.append((idx, passo["ts"], passo["texto"]))
                if primeiro is None:
                    primeiro = passo["texto"]
            elif passo["papel"] == "assistant" and passo["texto"]:
                respostas.append((idx, passo["texto"]))
            for bruto in passo["cwds"]:
                caminho = util.clean_path(bruto)
                if caminho:
                    declarados[caminho] += 1
    except sqlite3.Error as exc:
        report.warn("antigravity: leitura de steps falhou (%s)" % exc)

    try:
        existing = {r[0] for r in ro.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _BLOB_TABLES:
            if table not in existing:
                continue
            for row in ro.execute('SELECT * FROM "%s"' % table):
                for value in row:
                    if not isinstance(value, bytes):
                        continue
                    if model is None:
                        mm = _MODEL_RE.search(value)
                        if mm:
                            model = mm.group(1).decode("ascii", "ignore").rstrip(".-")
                    for raw in _PATH_RE.findall(value):
                        p = util.clean_path(raw.decode("utf-8", "ignore"))
                        if p:
                            candidates[p] += 1
    except sqlite3.Error as exc:
        report.warn("antigravity: leitura de blobs falhou (%s)" % exc)
    finally:
        ro.close()

    return {
        "cwd": _pick_workspace(candidates, roots, declarados),
        "model": model,
        "titulo": titulo,
        "primeiro": primeiro,
        "prompts": prompts,
        "respostas": respostas,
        "msgs": msgs,
        "inicio": min(datas) if datas else None,
        "fim": max(datas) if datas else None,
    }


def _depth(path):
    """Quantos componentes abaixo da letra do drive. 'E:\\' = 0, 'E:\\a\\b' = 2."""
    parts = [p for p in (path or "").split(util.SEP) if p and not p.endswith(":")]
    return len(parts)


def _pick_workspace(candidates, roots=(), declarados=None):
    """Escolhe o diretorio de projeto mais provavel entre os caminhos achados.

    Ha' DUAS fontes, e elas nao se misturam:

      1. o `Cwd` que a ferramenta DECLAROU numa chamada. Quando existe, decide
         sozinho -- e decide inclusive pelo NAO: se o unico diretorio declarado
         foi a pasta pessoal ou uma pasta que sumiu do disco, a resposta certa
         e' "projeto desconhecido". Deixar cair na heuristica aqui foi o que
         atribuiu uma conversa sobre a pasta pessoal a um projeto de jogo,
         so' porque o caminho dele aparecia nos blobs;
      2. sem `Cwd` declarado, a varredura de strings dos blobs -- frequencia
         pura, que continua sendo a unica pista disponivel.
    """
    root_keys = indexer.nunca_projeto(roots)
    if declarados:
        return _melhor(declarados, root_keys, roots)
    return _melhor(candidates, root_keys, roots)


def _melhor(pontos, root_keys, roots):
    """Sobe cada caminho ate' um diretorio real e devolve o mais pontuado.

    Duas guardas aprendidas na primeira varredura:

      * profundidade minima 2 -- sem isso a subida pelo dirname termina em 'E:\\'
        e a conversa inteira e' atribuida a raiz do disco;
      * bonus grande para quem esta' sob uma raiz configurada -- caminhos de
        dependencia ('chrome-win', ferramentas baixadas) aparecem muito nos blobs
        e senao ganham da pasta real do projeto.
    """
    scores = Counter()
    for path, count in pontos.items():
        low = path.lower()
        if any(n in low for n in _NOISE):
            continue
        probe = path
        for _ in range(6):
            if not probe or _depth(probe) < 2:
                break
            # A propria raiz configurada nao e' um projeto: continua subindo/
            # descartando, senao varias conversas colapsam em "Antigravity".
            if util.path_key(probe) in root_keys:
                break
            if os.path.isdir(probe):
                bonus = 100 if any(util.is_under(probe, r) for r in roots) else 0
                scores[probe] += count + bonus
                break
            probe = os.path.dirname(probe)
    if not scores:
        return None
    # Empate: vence o caminho mais curto -- tende a ser a raiz do projeto, nao
    # uma subpasta profunda dele.
    return max(scores.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


def _index_brain(con, resolver, report, root, native_id, sid, project_id):
    """brain\\<id>\\ guarda os markdowns legiveis e as imagens da conversa."""
    bdir = os.path.join(root, "brain", native_id)
    if not os.path.isdir(bdir):
        return
    if project_id is None:
        row = con.execute("SELECT project_id FROM sessions WHERE id=?", (sid,)).fetchone()
        project_id = row["project_id"] if row else None

    for dirpath, dirs, files in os.walk(bdir):
        dirs[:] = [d for d in dirs if d != "tempmediaStorage"]
        for name in files:
            low = name.lower()
            if low.endswith(".metadata.json"):
                continue
            full = os.path.join(dirpath, name)
            if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                kind = "image"
            elif low.endswith((".md", ".txt")):
                kind = "context"
            else:
                continue
            if indexer.add_artifact(con, full, kind, AI, project_id, sid, name):
                report.bump("antigravity_" + kind + "s")
