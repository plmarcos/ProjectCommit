"""Coletor do Codex.

Fonte primaria: %USERPROFILE%\\.codex\\state_N.sqlite, tabela 'threads' -- ja' traz
cwd, titulo, modelo, tokens_used, versao e datas mastigados. NUNCA listar a partir
dos rollouts: sao arquivos de ate' 515 MB.

Os rollouts (.codex\\sessions\\YYYY\\MM\\DD\\rollout-*.jsonl) sao lidos so' para
extrair os prompts do usuario, com prefiltro por bytes antes de qualquer
json.loads -- isso roda a ~430 MB/s, contra ~15 MB/s se parseasse tudo.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .. import db, indexer, media, settings, touched, transcript, util

AI = "codex"
KIND = "codex_rollout"

_USER_MARK = b'"role":"user"'
_ASSIST_MARK = b'"role":"assistant"'
_MAX_LINE = 8 * 1024 * 1024
#: Acima disto a linha nao e' um prompt digitado -- e' contexto injetado.
_MAX_PROMPT = 512 * 1024


def collect(con, resolver, report, progress=None):
    threads = _read_threads(con, resolver, report)
    if progress:
        progress(0, max(len(threads), 1), "Codex: threads")

    total = max(len(threads), 1)
    for i, th in enumerate(threads):
        if progress:
            progress(i, total, "Codex: " + (th["title"] or th["native_id"])[:40])
        if th["rollout"]:
            try:
                _collect_rollout(con, report, th)
            except Exception as exc:
                report.warn("codex rollout %s: %s" % (th["native_id"][:8], exc))
        con.commit()

    _index_generated_images(con, report)
    _index_chatgpt_projects(con, resolver, report)
    con.commit()
    if progress:
        progress(total, total, "Codex: concluido")


def _read_threads(con, resolver, report):
    """Le a tabela threads. Cai para session_index.jsonl se a base estiver travada."""
    state = settings.codex_state_db()
    out = []
    if not state or not os.path.isfile(state):
        report.warn("codex: state_*.sqlite nao encontrado")
        return out
    try:
        ro = db.open_readonly(state)
    except sqlite3.Error as exc:
        report.warn("codex: base travada (%s), usando session_index.jsonl" % exc)
        return _fallback_index(con, resolver, report)

    try:
        cols = {r[1] for r in ro.execute("PRAGMA table_info(threads)")}
        rows = ro.execute("SELECT * FROM threads").fetchall()
    except sqlite3.Error as exc:
        report.warn("codex: leitura de threads falhou (%s)" % exc)
        ro.close()
        return _fallback_index(con, resolver, report)

    def col(row, name, default=None):
        return row[name] if name in cols else default

    for row in rows:
        native_id = col(row, "id")
        if not native_id:
            continue
        cwd = util.clean_path(col(row, "cwd"))
        project_id = resolver.resolve(cwd) if cwd else None
        sid = AI + ":" + native_id
        rollout = util.clean_path(col(row, "rollout_path"))
        started = util.to_epoch(col(row, "created_at_ms") or col(row, "created_at"))
        ended = util.to_epoch(
            col(row, "recency_at_ms") or col(row, "updated_at_ms") or col(row, "updated_at")
        )
        extra = json.dumps(
            {
                "source": col(row, "source"),
                "provider": col(row, "model_provider"),
                "effort": col(row, "reasoning_effort"),
                "archived": col(row, "archived"),
                "origin": col(row, "git_origin_url"),
            },
            ensure_ascii=False,
        )
        indexer.merge_session(
            con, sid, AI, native_id, absolute=True,
            project_id=project_id, cwd=cwd,
            title=util.clip(col(row, "title") or col(row, "name"), 200),
            model=col(row, "model"), cli_version=col(row, "cli_version"),
            git_branch=col(row, "git_branch"),
            first_prompt=util.clip(col(row, "first_user_message") or col(row, "preview"), 500),
            source_path=rollout, started_at=started, ended_at=ended,
            size_bytes=(util.stat_or_none(rollout).st_size if rollout and os.path.isfile(rollout) else 0),
            tok_total=int(col(row, "tokens_used") or 0), extra=extra,
        )
        if started:
            indexer.add_event(
                con, "codex:start:" + native_id, AI, started, "session",
                util.clip(col(row, "title") or col(row, "first_user_message"), 300),
                session_id=sid, project_id=project_id,
            )
        out.append(
            {
                "native_id": native_id, "sid": sid, "project_id": project_id,
                "rollout": rollout if rollout and os.path.isfile(rollout) else None,
                "title": col(row, "title") or "",
            }
        )
        report.bump("codex_threads")
    ro.close()
    con.commit()
    return out


def _fallback_index(con, resolver, report):
    """session_index.jsonl so' tem id/nome/data -- melhor que nada."""
    path = os.path.join(settings.codex_dir(), "session_index.jsonl")
    seen = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("id"):
                    seen[rec["id"]] = rec
    except OSError:
        return []
    for native_id, rec in seen.items():
        sid = AI + ":" + native_id
        ts = util.to_epoch(rec.get("updated_at"))
        indexer.merge_session(
            con, sid, AI, native_id, title=util.clip(rec.get("thread_name"), 200), ended_at=ts
        )
        report.bump("codex_threads_fallback")
    con.commit()
    return []


def _collect_rollout(con, report, th):
    """Extrai prompts do usuario, incremental por offset."""
    path = th["rollout"]
    st = util.stat_or_none(path)
    if st is None:
        return
    prev = db.source_state(con, path)
    offset = 0
    if prev is not None:
        if prev["size"] == st.st_size and abs(prev["mtime"] - st.st_mtime) < 0.001:
            report.bump("codex_rollouts_skipped")
            return
        offset = prev["last_offset"] or 0
        if st.st_size < offset:
            offset = 0

    consumed = offset
    found = imgs = respostas = 0
    tocados = {}
    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            inicio = consumed          # onde ESTA linha comeca, para o media
            consumed += len(raw)
            if len(raw) > _MAX_LINE:
                continue

            # Tres prefiltros por BYTES antes de qualquer json.loads. 99% das
            # linhas sao saida de ferramenta e nao interessam a nenhum deles.
            #
            # O corte por TAMANHO no prompt e' o que mais rende: num rollout de
            # 865 MB so' 187 linhas passam o filtro de papel -- mas elas somam
            # 422 MB, porque cada "mensagem do usuario" carrega o historico
            # injetado junto. Um prompt digitado nunca tem meio mega; se tiver
            # imagem, passa mesmo assim.
            quer_prompt = _USER_MARK in raw and (
                len(raw) <= _MAX_PROMPT or media.MARCA_CODEX in raw
            )
            quer_patch = len(raw) <= _MAX_PROMPT and (
                touched.MARCA_CODEX in raw or touched.MARCA_CODEX_ALT in raw
            )
            quer_resposta = len(raw) <= _MAX_PROMPT and _ASSIST_MARK in raw
            if not (quer_prompt or quer_patch or quer_resposta):
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            ts = util.to_epoch(rec.get("timestamp"))

            if quer_patch:
                touched.acumular(tocados, touched.de_codex(rec), ts)

            if quer_resposta:
                fala = transcript.texto_de_resposta(AI, rec)
                if fala:
                    db.fts_replace(
                        con, "msg:%s:%d" % (th["sid"], inicio),
                        fala[:transcript.LIMITE_BUSCA],
                    )
                    respostas += 1

            if not quer_prompt:
                continue
            payload = rec.get("payload")
            if not isinstance(payload, dict) or payload.get("role") != "user":
                continue

            if media.MARCA_CODEX in raw:
                imgs += media.registrar(
                    con, th["sid"], th["project_id"], AI, path, inicio, len(raw),
                    ts, media.blocos_codex(rec))

            text = _text_of(payload.get("content"))
            if not text or text.lstrip().startswith("<"):
                continue  # contexto injetado pelo app, nao e' prompt digitado
            uid = "codex:%s:%s:%d" % (th["native_id"], payload.get("id") or "", consumed)
            if indexer.add_event(
                con, uid, AI, ts, "prompt", util.clip(text, 2000),
                session_id=th["sid"], project_id=th["project_id"],
            ):
                found += 1

    con.execute(
        "UPDATE sessions SET msg_count = msg_count + ? WHERE id=?", (found, th["sid"])
    )
    report.bump("codex_arquivos", touched.gravar(con, th["sid"], th["project_id"], AI, tocados))
    if offset == 0:
        report.completas.add(th["sid"])   # lido inteiro aqui: o backfill pode pular
    db.set_source_state(con, path, KIND, st.st_size, st.st_mtime, consumed)
    report.bump("codex_rollouts_indexed")
    report.bump("codex_prompts", found)
    if imgs:
        report.bump("codex_imagens", imgs)
    if respostas:
        report.bump("codex_respostas", respostas)


def _text_of(content):
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("input_text", "text", "output_text"):
                t = block.get("text")
                if t:
                    parts.append(t)
        return "\n".join(parts).strip() or None
    return None


def _index_generated_images(con, report):
    root = os.path.join(settings.codex_dir(), "generated_images")
    if not os.path.isdir(root):
        return
    for dirpath, _dirs, files in os.walk(root):
        thread = os.path.basename(dirpath)
        sid = AI + ":" + thread if thread != "generated_images" else None
        row = con.execute("SELECT project_id FROM sessions WHERE id=?", (sid,)).fetchone() if sid else None
        pid = row["project_id"] if row else None
        for name in files:
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                if indexer.add_artifact(
                    con, os.path.join(dirpath, name), "image", AI, pid, sid, name
                ):
                    report.bump("codex_images")


def _index_chatgpt_projects(con, resolver, report):
    """.chatgpt-projects guarda AGENTS.md, planos e anexos por projeto do ChatGPT."""
    root = os.path.join(settings.codex_dir(), ".chatgpt-projects")
    if not os.path.isdir(root):
        return
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__")]
        for name in files:
            if name.lower().endswith((".md", ".txt")):
                if indexer.add_artifact(
                    con, os.path.join(dirpath, name), "context", AI, None, None, name
                ):
                    report.bump("codex_contexts")
