"""Leitura da conversa de uma sessao, paginada.

O problema: um rollout do Codex chega a 491 MB e um JSONL do Claude a 90 MB.
Reler o arquivo inteiro a cada pagina seria inviavel.

A solucao e' um indice de mensagens por sessao (`msg_index`): uma unica passada
guarda o deslocamento em bytes, o tamanho e o papel de cada mensagem. Depois,
mostrar a pagina N e' um punhado de `seek()` -- nao importa o tamanho do arquivo.

Como o JSONL e' append-only, o indice tambem cresce por offset: se o arquivo
aumentou, continuamos de onde paramos em vez de refazer tudo.

O Antigravity entra pelo mesmo msg_index, com uma diferenca: a conversa dele
nao e' um arquivo de texto, e' uma tabela `steps` de blobs protobuf. Ali a coluna
`offset` guarda o indice do passo e o corpo e' decodificado na hora por
core/agsteps.py -- ver a docstring de _index_antigravity().
"""
from __future__ import annotations

import json
import os
import time

from . import agsteps, db, util

# Uma linha de rollout pode ter megabytes de saida de ferramenta. Lemos ate' este
# limite para montar a previa; o corpo completo e' cortado na exibicao.
_MAX_LINE = 4 * 1024 * 1024
_PREVIEW = 160


def _claude_message(rec):
    """(papel, texto, ferramentas) de um registro do Claude, ou None se nao for mensagem."""
    rtype = rec.get("type")
    if rtype not in ("user", "assistant"):
        return None
    message = rec.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    partes, ferramentas = [], []
    if isinstance(content, str):
        partes.append(content)
    elif isinstance(content, list):
        for bloco in content:
            if not isinstance(bloco, dict):
                continue
            tipo = bloco.get("type")
            if tipo == "text" and bloco.get("text"):
                partes.append(bloco["text"])
            elif tipo == "thinking" and bloco.get("thinking"):
                partes.append("∴ " + bloco["thinking"])
            elif tipo == "tool_use":
                ferramentas.append(bloco.get("name") or "ferramenta")
            elif tipo == "tool_result":
                ferramentas.append("resultado")

    texto = "\n\n".join(p for p in partes if p).strip()
    papel = rtype
    if rtype == "user":
        if rec.get("isSidechain"):
            papel = "subagente"
        elif not texto and ferramentas:
            papel = "ferramenta"       # so' devolveu resultado, ninguem digitou nada
        elif _injetado(texto):
            papel = "contexto"
    return papel, texto, ferramentas


def _injetado(texto):
    """Texto que a ferramenta injeta como se fosse do usuario.

    Sem isto o leitor abre a conversa mostrando `<recommended_plugins>` ou um
    lembrete de sistema no lugar do que a pessoa realmente escreveu.
    """
    if not texto:
        return False
    t = texto.lstrip()
    return (
        t.startswith("<")
        or t.startswith("[Request interrupted")
        or t.startswith("This session is being continued")
        or t.startswith("Caveat: The messages below")
    )


def _codex_message(rec):
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "message":
        return None
    papel = payload.get("role")
    if papel not in ("user", "assistant"):
        return None  # 'developer' e' contexto injetado pelo app, nao conversa

    content = payload.get("content")
    partes = []
    if isinstance(content, str):
        partes.append(content)
    elif isinstance(content, list):
        for bloco in content:
            if isinstance(bloco, dict) and bloco.get("text"):
                partes.append(bloco["text"])
    texto = "\n\n".join(partes).strip()
    if papel == "user" and _injetado(texto):
        papel = "contexto"
    return papel, texto, []


PARSERS = {"claude": _claude_message, "codex": _codex_message}

#: Corte por mensagem ao alimentar a busca. A media medida e' de 350 caracteres
#: -- este teto so' apara o discurso longo, que ja' fica achavel pelo comeco.
LIMITE_BUSCA = 1500


def texto_de_resposta(ai, rec):
    """O que a IA RESPONDEU, para a busca. None se a linha nao for resposta.

    Existe porque o FTS so' alcancava a fala do usuario: 1.232 de 78.693
    mensagens. Reaproveita os parsers do leitor em vez de repetir a extracao --
    se a forma do registro mudar, muda num lugar so'.
    """
    parser = PARSERS.get(ai)
    if parser is None:
        return None
    lido = parser(rec)
    if not lido:
        return None
    papel, texto, _ferramentas = lido
    return texto if papel == "assistant" and texto else None


def precisa_indexar(con, session):
    """Quantos bytes faltam indexar desta conversa. 0 = pronta para abrir.

    Existe para a interface saber, ANTES de pedir a pagina, se a abertura vai ser
    instantanea ou vai levar dezenas de segundos. Medido: a maior conversa deste
    PC (rollout de 1,6 GB) leva 41 s na primeira abertura e 45 ms nas seguintes.
    """
    path = session["source_path"]
    if not path or not os.path.isfile(path):
        return 0
    st = util.stat_or_none(path)
    if st is None:
        return 0
    row = con.execute(
        "SELECT indexed_size, indexed_offset FROM msg_state WHERE session_id=?",
        (session["id"],),
    ).fetchone()
    if session["ai"] == "antigravity":
        # Blob lido inteiro de uma vez; ou esta' pronto, ou refaz tudo.
        return 0 if (row and row["indexed_size"] == st.st_size) else st.st_size
    if not row:
        return st.st_size
    if row["indexed_size"] == st.st_size:
        return 0
    if st.st_size < row["indexed_size"]:
        return st.st_size
    return st.st_size - (row["indexed_offset"] or 0)


def build_index(con, session, progresso=None):
    """Garante que msg_index cobre o arquivo inteiro. Devolve o total de mensagens.

    Incremental: se o arquivo cresceu desde a ultima vez, indexa so' o pedaco novo.
    `progresso(bytes_lidos, bytes_totais)` e' chamado durante a leitura, para a
    interface poder mostrar barra em vez de congelar por 41 segundos.
    """
    sid, ai = session["id"], session["ai"]
    path = session["source_path"]
    if ai == "antigravity":
        return _index_antigravity(con, sid, path)
    parser = PARSERS.get(ai)
    if parser is None or not path or not os.path.isfile(path):
        return 0

    st = util.stat_or_none(path)
    if st is None:
        return 0

    row = con.execute(
        "SELECT indexed_size, indexed_offset, count FROM msg_state WHERE session_id=?", (sid,)
    ).fetchone()
    offset = seq = 0
    if row:
        if row["indexed_size"] == st.st_size:
            return row["count"]
        if st.st_size < row["indexed_size"]:
            # Arquivo encolheu: refaz do zero.
            con.execute("DELETE FROM msg_index WHERE session_id=?", (sid,))
        else:
            offset, seq = row["indexed_offset"], row["count"]

    lote = []
    proximo_aviso = offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        pos = offset
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # linha ainda sendo escrita: nao consome o offset
            tamanho = len(raw)
            inicio = pos
            pos += tamanho
            # A cada 8 MB, e nao por linha: num rollout de 1,6 GB sao milhoes de
            # linhas, e avisar em todas custaria mais que a propria leitura.
            if progresso is not None and pos >= proximo_aviso:
                proximo_aviso = pos + 8 * 1024 * 1024
                progresso(pos, st.st_size)
            if tamanho > _MAX_LINE:
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            achado = parser(rec)
            if achado is None:
                continue
            papel, texto, ferramentas = achado
            if not texto and not ferramentas:
                continue
            previa = util.clip(texto, _PREVIEW) or ("→ " + ", ".join(ferramentas[:3]))
            lote.append(
                (sid, seq, inicio, tamanho, papel,
                 util.to_epoch(rec.get("timestamp")), previa)
            )
            seq += 1
            if len(lote) >= 500:
                con.executemany(
                    "INSERT OR REPLACE INTO msg_index"
                    "(session_id, seq, offset, length, role, ts, preview) VALUES(?,?,?,?,?,?,?)",
                    lote,
                )
                lote = []

    if lote:
        con.executemany(
            "INSERT OR REPLACE INTO msg_index"
            "(session_id, seq, offset, length, role, ts, preview) VALUES(?,?,?,?,?,?,?)",
            lote,
        )
    con.execute(
        "INSERT INTO msg_state(session_id, indexed_size, indexed_offset, count, built_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
        "indexed_size=excluded.indexed_size, indexed_offset=excluded.indexed_offset, "
        "count=excluded.count, built_at=excluded.built_at",
        (sid, st.st_size, pos, seq, util.now()),
    )
    con.commit()
    return seq


def _index_antigravity(con, sid, path):
    """O mesmo msg_index, para uma conversa que nao e' um arquivo de texto.

    Aqui a coluna `offset` guarda `steps.idx` e `length` fica em 0: nao ha'
    deslocamento em bytes num blob. O corpo e' relido na hora de exibir --
    os 12 bancos somam 58 MB (o maior tem 24), entao decodificar meia duzia de
    passos por pagina e' instantaneo e nada e' copiado para o disco.
    """
    if not path or not os.path.isfile(path):
        return 0
    st = util.stat_or_none(path)
    if st is None:
        return 0

    row = con.execute(
        "SELECT indexed_size, count FROM msg_state WHERE session_id=?", (sid,)
    ).fetchone()
    if row and row["indexed_size"] == st.st_size:
        return row["count"]

    try:
        ro = db.open_readonly(path)
    except Exception:
        return 0
    try:
        _cabecalho, mensagens = agsteps.percorrer(ro)
    finally:
        ro.close()

    con.execute("DELETE FROM msg_index WHERE session_id=?", (sid,))
    con.executemany(
        "INSERT OR REPLACE INTO msg_index"
        "(session_id, seq, offset, length, role, ts, preview) VALUES(?,?,?,?,?,?,?)",
        [
            (sid, seq, idx, 0, passo["papel"], passo["ts"],
             util.clip(passo["texto"], _PREVIEW) or ("→ " + ", ".join(passo["ferramentas"][:3])))
            for seq, (idx, passo) in enumerate(mensagens)
        ],
    )
    con.execute(
        "INSERT INTO msg_state(session_id, indexed_size, indexed_offset, count, built_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
        "indexed_size=excluded.indexed_size, indexed_offset=excluded.indexed_offset, "
        "count=excluded.count, built_at=excluded.built_at",
        (sid, st.st_size, 0, len(mensagens), util.now()),
    )
    con.commit()
    return len(mensagens)


def export_markdown(con, session, roles=None, max_chars=200000):
    """Escreve a conversa inteira num .md e devolve o caminho.

    Grava em %LOCALAPPDATA%\\ProjectCommit\\exports -- fora do projeto, entao nao
    passa pelo Modo Seguro. Devolve um arquivo de verdade em vez de um download
    do navegador: assim funciona igual dentro da janela WebView e fica guardado.
    """
    from . import settings

    destino = os.path.join(settings.data_dir(), "exports")
    os.makedirs(destino, exist_ok=True)

    titulo = session["title"] or session["native_id"]
    seguro = "".join(c if (c.isalnum() or c in " -_") else "_" for c in titulo).strip()[:60]
    carimbo = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(session["ended_at"] or util.now()))
    caminho = os.path.join(destino, "%s - %s.md" % (carimbo, seguro or "conversa"))

    escritas = 0
    with open(caminho, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# %s\n\n" % titulo)
        fh.write("- **IA:** %s\n" % session["ai"])
        if session["model"]:
            fh.write("- **Modelo:** %s\n" % session["model"])
        if session["cwd"]:
            fh.write("- **Projeto:** `%s`\n" % session["cwd"])
        for rotulo, campo in (("Início", "started_at"), ("Fim", "ended_at")):
            if session[campo]:
                fh.write("- **%s:** %s\n" % (
                    rotulo, time.strftime("%d/%m/%Y %H:%M", time.localtime(session[campo]))))
        fh.write("- **Origem:** `%s`\n\n---\n\n" % (session["source_path"] or "—"))

        offset = 0
        while True:
            pg = page(con, session, offset, 200, max_chars=max_chars, roles=roles)
            if not pg["messages"]:
                break
            for m in pg["messages"]:
                quem = {"user": "Você", "assistant": "IA", "ferramenta": "Ferramenta",
                        "contexto": "Contexto", "subagente": "Subagente"}.get(m["role"], m["role"])
                hora = (time.strftime("%d/%m %H:%M", time.localtime(m["ts"])) if m["ts"] else "")
                fh.write("### %s%s\n\n" % (quem, (" · " + hora) if hora else ""))
                if m["text"]:
                    fh.write(m["text"].rstrip() + "\n\n")
                if m["tools"]:
                    fh.write("`%s`\n\n" % "` `".join(m["tools"]))
                if m["truncated"]:
                    fh.write("_(mensagem cortada)_\n\n")
                escritas += 1
            offset += len(pg["messages"])
            if offset >= pg["total"]:
                break

    return {"path": caminho, "messages": escritas}


#: Papeis que contam como "conversa" -- o padrao do leitor.
CONVERSA = ("user", "assistant")
TODOS = ("user", "assistant", "ferramenta", "contexto", "subagente")


def _corpos_jsonl(path, rows, parser, max_chars):
    """Le por seek as mensagens pedidas do JSONL -- nao importa o tamanho dele."""
    mensagens = []
    with open(path, "rb") as fh:
        for r in rows:
            fh.seek(r["offset"])
            raw = fh.read(r["length"])
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            achado = parser(rec)
            if achado is None:
                continue
            papel, texto, ferramentas = achado
            mensagens.append(
                {
                    "seq": r["seq"], "role": papel, "ts": r["ts"],
                    "text": texto[:max_chars], "truncated": len(texto) > max_chars,
                    "tools": ferramentas[:12],
                }
            )
    return mensagens


def _corpos_antigravity(path, rows, max_chars):
    """Decodifica os passos pedidos direto do blob, em modo somente leitura."""
    if not rows:
        return []
    indices = [r["offset"] for r in rows]
    try:
        ro = db.open_readonly(path)
    except Exception:
        return []
    try:
        marcadores = ",".join("?" * len(indices))
        cru = {
            idx: (tipo, payload)
            for idx, tipo, payload in ro.execute(
                "SELECT idx, step_type, step_payload FROM steps WHERE idx IN (%s)" % marcadores,
                indices,
            )
        }
    except Exception:
        return []
    finally:
        ro.close()

    mensagens = []
    for r in rows:
        tipo_payload = cru.get(r["offset"])
        if not tipo_payload:
            continue
        passo = agsteps.ler(tipo_payload[0], tipo_payload[1])
        if passo is None:
            continue
        texto = passo["texto"] or ""
        mensagens.append(
            {
                "seq": r["seq"], "role": passo["papel"], "ts": r["ts"] or passo["ts"],
                "text": texto[:max_chars], "truncated": len(texto) > max_chars,
                "tools": passo["ferramentas"][:12],
            }
        )
    return mensagens


def page(con, session, offset=0, limit=40, max_chars=12000, roles=None, at=None):
    """Uma fatia da conversa. Le so' as linhas pedidas, por seek.

    `roles` filtra no SQL, nao no cliente: numa sessao de 4553 registros apenas
    177 sao fala do usuario, entao filtrar depois de paginar traria paginas quase
    vazias.
    """
    ai = session["ai"]
    bruto = build_index(con, session)
    parser = PARSERS.get(ai)
    path = session["source_path"]
    if not bruto or (parser is None and ai != "antigravity"):
        return {"ai": ai, "total": 0, "offset": 0, "limit": limit, "messages": [],
                "unavailable": "Arquivo de origem indisponivel."}

    papeis = [r for r in (roles or CONVERSA) if r in TODOS] or list(CONVERSA)
    marcadores = ",".join("?" * len(papeis))

    total = con.execute(
        "SELECT COUNT(*) FROM msg_index WHERE session_id=? AND role IN (%s)" % marcadores,
        [session["id"]] + papeis,
    ).fetchone()[0]

    if at is not None:
        # Posicao da mensagem pedida DENTRO da lista ja' filtrada por papel --
        # e' o que faz um resultado de busca abrir na mensagem certa em vez de
        # no comeco da conversa.
        antes = con.execute(
            "SELECT COUNT(*) FROM msg_index WHERE session_id=? AND role IN (%s) "
            "AND seq < COALESCE((SELECT seq FROM msg_index WHERE session_id=? AND offset=?), -1)"
            % marcadores,
            [session["id"]] + papeis + [session["id"], int(at)],
        ).fetchone()[0]
        offset = max(0, (antes // limit) * limit)

    offset = max(0, min(int(offset), max(total - 1, 0)))
    rows = con.execute(
        "SELECT seq, offset, length, role, ts FROM msg_index "
        "WHERE session_id=? AND role IN (%s) ORDER BY seq LIMIT ? OFFSET ?" % marcadores,
        [session["id"]] + papeis + [limit, offset],
    ).fetchall()

    if ai == "antigravity":
        mensagens = _corpos_antigravity(path, rows, max_chars)
    else:
        mensagens = _corpos_jsonl(path, rows, parser, max_chars)

    return {
        "ai": ai, "total": total, "raw_total": bruto, "offset": offset, "limit": limit,
        "roles": papeis, "messages": mensagens, "unavailable": None,
    }
