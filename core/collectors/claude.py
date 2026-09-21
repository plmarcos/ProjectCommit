"""Coletor do Claude Code.

Fonte: %USERPROFILE%\\.claude\\projects\\<slug>\\<sessionId>.jsonl
Um arquivo = uma sessao. Os arquivos sao append-only, entao a leitura e'
incremental por offset em bytes: guardamos onde paramos e damos seek() ali.
Sao ~700 arquivos / 574 MB -- nunca ler o arquivo inteiro na memoria.
"""
from __future__ import annotations

import json
import os
import time

from .. import db, indexer, media, settings, touched, transcript, util

AI = "claude"
KIND = "claude_jsonl"

#: Linha de resposta e' pequena; acima disto e' saida de ferramenta.
_MAX_RESPOSTA = 1024 * 1024
_ASSIST_MARK = b'"assistant"'

# Tipos que nao carregam informacao util para o indice.
_SKIP = {"attachment", "mode", "queue-operation", "file-history-snapshot"}


def _text_of(message):
    """message.content vem como str ou como lista de blocos."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if t:
                    parts.append(t)
        return "\n".join(parts).strip() or None
    return None


def _usage_of(message):
    u = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(u, dict):
        return (0, 0, 0, 0)
    return (
        int(u.get("input_tokens") or 0),
        int(u.get("output_tokens") or 0),
        int(u.get("cache_read_input_tokens") or 0),
        int(u.get("cache_creation_input_tokens") or 0),
    )


def _session_files():
    root = settings.claude_projects_dir()
    if not os.path.isdir(root):
        return
    for slug in sorted(os.listdir(root)):
        sdir = os.path.join(root, slug)
        if not os.path.isdir(sdir):
            continue
        for name in sorted(os.listdir(sdir)):
            if name.endswith(".jsonl"):
                yield slug, os.path.join(sdir, name)


def collect(con, resolver, report, progress=None):
    files = list(_session_files())
    total = len(files)
    for i, (slug, path) in enumerate(files):
        if progress:
            progress(i, total, "Claude: " + os.path.basename(path))
        try:
            _collect_file(con, resolver, report, slug, path)
        except Exception as exc:  # um arquivo corrompido nao pode derrubar a varredura
            report.warn("claude %s: %s" % (os.path.basename(path), exc))
        if i % 25 == 0:
            con.commit()
    con.commit()
    if progress:
        progress(total, total, "Claude: concluido")


def _collect_file(con, resolver, report, slug, path):
    st = util.stat_or_none(path)
    if st is None:
        return
    native_id = os.path.splitext(os.path.basename(path))[0]
    sid = AI + ":" + native_id

    prev = db.source_state(con, path)
    offset = 0
    if prev is not None:
        if prev["size"] == st.st_size and abs(prev["mtime"] - st.st_mtime) < 0.001:
            report.bump("claude_files_skipped")
            # O arquivo pai nao mudou, mas um subagente pode ter sido gravado
            # depois do ultimo flush dele -- entao a checagem continua.
            _merge_subagent_delta(con, os.path.dirname(path), native_id, sid, report)
            return
        offset = prev["last_offset"] or 0
        if st.st_size < offset:  # arquivo encolheu: reindexar do zero
            offset = 0
            report.log("claude: %s encolheu, reindexando" % os.path.basename(path))

    # Reler do ZERO um arquivo que ja' foi contado dobra tudo, porque
    # merge_session() e add_daily_usage() SOMAM -- e' assim que a leitura
    # incremental funciona. Medido numa sessao real antes da correcao: o indice
    # dizia 428.232.892 tokens contra 221.103.397 relendo, exatamente o pai
    # contado duas vezes (os subagentes escapavam por terem estado proprio).
    # Entao quem le' do zero zera antes -- a sessao E' o estado dos subagentes.
    if offset == 0:
        _esquecer(con, sid, os.path.dirname(path), native_id)

    agg = {
        "msg_count": 0, "tok_in": 0, "tok_out": 0, "tok_cache_r": 0, "tok_cache_w": 0,
        "cwd": None, "title": None, "model": None, "cli_version": None,
        "git_branch": None, "first_prompt": None,
        "started_at": None, "ended_at": None,
    }
    custom_title = None
    prompts = []
    diario = {}          # (dia, ia, modelo, projeto) -> consumo
    imagens = []         # (offset, length, ts, blocos) das imagens coladas
    tocados = {}         # caminho -> quantas vezes a IA escreveu nele
    respostas = 0
    consumed = offset

    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            # Linha possivelmente truncada no fim do arquivo (IA ainda escrevendo):
            # nao consumir o offset dela, para reler na proxima passada.
            if not raw.endswith(b"\n"):
                break
            inicio = consumed          # onde ESTA linha comeca, para o media
            consumed += len(raw)
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                report.bump("claude_bad_lines")
                continue
            _absorb(rec, agg, prompts, diario)
            # Imagem colada mora dentro desta linha, em base64 -- registramos a
            # referencia agora, aproveitando que a linha ja' esta' parseada.
            if media.MARCA_CLAUDE in raw:
                blocos = media.blocos_claude(rec)
                if blocos:
                    imagens.append((inicio, len(raw), util.to_epoch(rec.get("timestamp")), blocos))
            # Qual ARQUIVO a IA escreveu. Mesmo molde da imagem: a linha ja'
            # esta' parseada, entao o prefiltro por bytes so' evita a chamada.
            if touched.MARCA_CLAUDE in raw:
                touched.acumular(
                    tocados, touched.de_claude(rec), util.to_epoch(rec.get("timestamp"))
                )
            # O que a IA respondeu tambem vai para a busca -- antes so' a fala
            # do usuario entrava, 1.232 de 78.693 mensagens.
            if _ASSIST_MARK in raw and len(raw) <= _MAX_RESPOSTA:
                fala = transcript.texto_de_resposta(AI, rec)
                if fala:
                    db.fts_replace(con, "msg:%s:%d" % (sid, inicio), fala[:transcript.LIMITE_BUSCA])
                    respostas += 1
            if rec.get("type") == "custom-title" and rec.get("customTitle"):
                custom_title = rec["customTitle"]

    if custom_title:
        agg["title"] = custom_title

    cwd = agg["cwd"] or util.slug_to_path(slug)
    project_id = resolver.resolve(cwd) if cwd else None

    # Subagentes vivem em <sessionId>\subagents\**\*.jsonl. Nao sao sessoes
    # proprias -- sao ramos da sessao pai -- entao o consumo deles entra aqui.
    subs = _collect_subagents(con, os.path.dirname(path), native_id, report)

    indexer.merge_session(
        con, sid, AI, native_id,
        project_id=project_id, cwd=util.clean_path(cwd), title=agg["title"],
        model=agg["model"], cli_version=agg["cli_version"], git_branch=agg["git_branch"],
        first_prompt=agg["first_prompt"], source_path=path, size_bytes=st.st_size,
        started_at=agg["started_at"], ended_at=agg["ended_at"],
        msg_count=agg["msg_count"], sub_count=subs["files"],
        tok_in=agg["tok_in"] + subs["tok_in"],
        tok_out=agg["tok_out"] + subs["tok_out"],
        tok_cache_r=agg["tok_cache_r"] + subs["tok_cache_r"],
        tok_cache_w=agg["tok_cache_w"] + subs["tok_cache_w"],
        # tok_total conta tokens REALMENTE processados: entrada + saida + escrita
        # de cache. cache_read fica de fora (e' releitura do mesmo prefixo, cobrada
        # a 1/10 e somada a cada turno -- incluir inflaria o total para bilhoes).
        tok_total=(
            agg["tok_in"] + agg["tok_out"] + agg["tok_cache_w"]
            + subs["tok_in"] + subs["tok_out"] + subs["tok_cache_w"]
        ),
    )

    for uid, ts, text in prompts:
        indexer.add_event(con, uid, AI, ts, "prompt", text, session_id=sid, project_id=project_id)

    # O projeto so' e' conhecido aqui; os baldes foram montados sem ele.
    indexer.add_daily_usage(
        con, {(dia, ia, modelo, project_id): v for (dia, ia, modelo, _), v in diario.items()},
        session_id=sid,
    )

    for off, ln, ts, blocos in imagens:
        report.bump("claude_imagens", media.registrar(
            con, sid, project_id, AI, path, off, ln, ts, blocos))

    report.bump("claude_arquivos", touched.gravar(con, sid, project_id, AI, tocados))
    if respostas:
        report.bump("claude_respostas", respostas)
    if offset == 0:
        report.completas.add(sid)   # lido inteiro aqui: o backfill nao precisa reler

    _index_tool_results(con, path, native_id, sid, project_id, report)

    db.set_source_state(con, path, KIND, st.st_size, st.st_mtime, consumed)
    report.bump("claude_files_indexed")
    report.bump("claude_prompts", len(prompts))


def _absorb(rec, agg, prompts, diario=None):
    rtype = rec.get("type")
    ts = util.to_epoch(rec.get("timestamp"))
    if ts:
        agg["started_at"] = ts if agg["started_at"] is None else min(agg["started_at"], ts)
        agg["ended_at"] = ts if agg["ended_at"] is None else max(agg["ended_at"], ts)

    for field, key in (("cwd", "cwd"), ("git_branch", "gitBranch"), ("cli_version", "version")):
        if rec.get(key):
            agg[field] = rec[key]

    if rtype == "ai-title" and rec.get("aiTitle"):
        agg["title"] = rec["aiTitle"]
        return
    if rtype == "custom-title":
        return
    if rtype in _SKIP:
        return

    message = rec.get("message")
    if rtype == "assistant" and isinstance(message, dict):
        if message.get("model"):
            agg["model"] = message["model"]
        tin, tout, tcr, tcw = _usage_of(message)
        agg["tok_in"] += tin
        agg["tok_out"] += tout
        agg["tok_cache_r"] += tcr
        agg["tok_cache_w"] += tcw
        agg["msg_count"] += 1
        if diario is not None and ts:
            # Hora LOCAL: o gráfico é lido no fuso de quem trabalhou, não em UTC.
            dia = time.strftime("%Y-%m-%d", time.localtime(ts))
            chave = (dia, AI, message.get("model") or "", None)
            b = diario.setdefault(chave, {"in": 0, "out": 0, "cr": 0, "cw": 0, "n": 0})
            b["in"] += tin; b["out"] += tout; b["cr"] += tcr; b["cw"] += tcw; b["n"] += 1
        return

    if rtype == "user" and isinstance(message, dict):
        agg["msg_count"] += 1
        if rec.get("isSidechain"):  # conversa de subagente, nao e' prompt do usuario
            return
        text = _text_of(message)
        if not text or text.startswith("<") or text.startswith("[Request interrupted"):
            return
        if agg["first_prompt"] is None:
            agg["first_prompt"] = util.clip(text, 500)
        uid = "claude:%s:%s" % (rec.get("sessionId"), rec.get("uuid") or ts)
        prompts.append((uid, ts, util.clip(text, 2000)))


def _esquecer(con, sid, session_dir, native_id):
    """Apaga o que foi acumulado desta sessao, antes de uma leitura do zero.

    Os subagentes tambem precisam esquecer: o consumo deles esta' somado na
    sessao que acabou de ser zerada, e cada um so' e' relido se o proprio estado
    em source_files sumir.
    """
    indexer.zerar_sessao(con, sid)
    # INSTR em vez de LIKE: caminho do Windows esta' cheio de '_' e o LIKE trata
    # isso como coringa, o que exigiria escapar tudo a mao.
    raiz = os.path.join(session_dir, native_id, "subagents") + os.sep
    con.execute(
        "DELETE FROM source_files WHERE kind='claude_subagent' "
        "AND INSTR(LOWER(path), LOWER(?)) = 1",
        (raiz,),
    )


def _merge_subagent_delta(con, session_dir, native_id, sid, report):
    """Aplica so' o que apareceu de novo nos subagentes de uma sessao ja' indexada."""
    subs = _collect_subagents(con, session_dir, native_id, report)
    if not any(subs.values()):
        return
    indexer.merge_session(
        con, sid, AI, native_id,
        sub_count=subs["files"], tok_in=subs["tok_in"], tok_out=subs["tok_out"],
        tok_cache_r=subs["tok_cache_r"], tok_cache_w=subs["tok_cache_w"],
        tok_total=subs["tok_in"] + subs["tok_out"] + subs["tok_cache_w"],
    )


def _collect_subagents(con, session_dir, native_id, report):
    """Soma o consumo dos subagentes da sessao (inclui os de workflow, mais fundos).

    Sao ~660 arquivos no total; a leitura tambem e' incremental por offset e so'
    procura blocos de usage -- o texto do subagente nao vai para o indice.
    """
    out = {"files": 0, "tok_in": 0, "tok_out": 0, "tok_cache_r": 0, "tok_cache_w": 0}
    root = os.path.join(session_dir, native_id, "subagents")
    if not os.path.isdir(root):
        return out

    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(dirpath, name)
            st = util.stat_or_none(full)
            if st is None:
                continue
            prev = db.source_state(con, full)
            offset = 0
            if prev is not None:
                if prev["size"] == st.st_size and abs(prev["mtime"] - st.st_mtime) < 0.001:
                    continue
                offset = prev["last_offset"] or 0
                if st.st_size < offset:
                    offset = 0
            else:
                out["files"] += 1

            consumed = offset
            try:
                with open(full, "rb") as fh:
                    fh.seek(offset)
                    for raw in fh:
                        if not raw.endswith(b"\n"):
                            break
                        consumed += len(raw)
                        if b'"usage"' not in raw:  # prefiltro barato
                            continue
                        try:
                            rec = json.loads(raw.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        if rec.get("type") != "assistant":
                            continue
                        tin, tout, tcr, tcw = _usage_of(rec.get("message"))
                        out["tok_in"] += tin
                        out["tok_out"] += tout
                        out["tok_cache_r"] += tcr
                        out["tok_cache_w"] += tcw
            except OSError as exc:
                report.warn("claude subagente %s: %s" % (name, exc))
                continue
            db.set_source_state(con, full, "claude_subagent", st.st_size, st.st_mtime, consumed)
            report.bump("claude_subagentes")
    return out


def _index_tool_results(con, jsonl_path, native_id, sid, project_id, report):
    """<sessionId>\\tool-results\\* sao os anexos/contexto daquela sessao."""
    tdir = os.path.join(os.path.dirname(jsonl_path), native_id, "tool-results")
    if not os.path.isdir(tdir):
        return
    for name in os.listdir(tdir):
        full = os.path.join(tdir, name)
        if os.path.isfile(full):
            kind = "image" if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) else "context"
            if indexer.add_artifact(con, full, kind, AI, project_id, sid, name):
                report.bump("claude_artifacts")
