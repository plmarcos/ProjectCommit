"""API HTTP local (Flask) consumida pela janela WebView.

Escuta so' em 127.0.0.1 numa porta efemera. Nao ha' autenticacao porque nao ha'
superficie de rede: a porta e' sorteada a cada execucao e nunca sai da maquina.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

from flask import Flask, jsonify, request, send_file, send_from_directory

from . import (actions, db, dossie, gitops, indexer, media, metas, radar,
               scanner, settings, sinais, touched, transcript, util)

SPARK_DAYS = 30

#: Acima disto a indexacao de uma conversa vira trabalho com progresso. Abaixo,
#: o ida-e-volta custaria mais que simplesmente indexar: 30 MB saem em ~0,3 s.
_INDEX_ASSINCRONO = 30 * 1024 * 1024


def resource_dir():
    """Pasta web/, tanto rodando do fonte quanto empacotado pelo PyInstaller."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "web")


class IndexJob:
    """Indexacao de UMA conversa, em thread, com progresso legivel.

    Existe por causa de um numero medido: abrir a maior conversa deste PC (um
    rollout de 1,6 GB) levava 41 segundos, durante os quais a gaveta mostrava
    uma barrinha de esqueleto de 4px e mais nada -- sem progresso, sem dizer o
    que estava acontecendo, sem cancelar. Parecia travado. Na segunda abertura,
    45 ms, porque o msg_index ja' existe.

    Mesmo molde do ScanJob, de proposito: um trabalho por vez, estado lido por
    polling. Nao ha' cancelamento real (a leitura e' um laco apertado sobre o
    arquivo), mas fechar a gaveta deixa de travar a interface.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.state = {
            "running": False, "sid": None, "lidos": 0, "total": 0,
            "erro": None, "mensagens": 0, "started_at": None,
        }

    def start(self, sid):
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(
                running=True, sid=sid, lidos=0, total=0, erro=None,
                mensagens=0, started_at=util.now(),
            )
        self.thread = threading.Thread(target=self._run, args=(sid,), daemon=True)
        self.thread.start()
        return True

    def _run(self, sid):
        con = None
        try:
            con = db.connect()
            row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise ValueError("sessao nao encontrada")
            self.state["total"] = transcript.precisa_indexar(con, row)

            def progresso(lidos, total):
                self.state.update(lidos=lidos, total=total)

            self.state["mensagens"] = transcript.build_index(con, row, progresso=progresso)
        except Exception as exc:
            self.state["erro"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if con is not None:
                con.close()
            self.state.update(running=False)


class ScanJob:
    """Uma varredura por vez, em thread, com progresso legivel pela interface."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.state = {
            "running": False, "done": 0, "total": 0, "label": "",
            "started_at": None, "finished_at": None, "error": None,
            "counts": {}, "warnings": [], "timings": {},
        }

    def start(self, measure=False, with_git=True):
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(
                running=True, done=0, total=0, label="iniciando",
                started_at=util.now(), finished_at=None, error=None,
                counts={}, warnings=[], timings={},
            )
        self.thread = threading.Thread(
            target=self._run, args=(measure, with_git), daemon=True
        )
        self.thread.start()
        return True

    def _run(self, measure, with_git):
        con = None
        try:
            con = db.init(db.connect())
            report = indexer.Report()

            def progress(done, total, label):
                self.state.update(done=done, total=total, label=label)

            scanner.full_scan(
                con, report=report, progress=progress, with_git=with_git, measure=measure
            )
            self.state["counts"] = report.counts
            self.state["warnings"] = report.warnings[:50]
            self.state["timings"] = report.timings
        except Exception as exc:
            self.state["error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if con is not None:
                con.close()
            self.state.update(running=False, finished_at=util.now(), label="concluido")


def create_app():
    app = Flask(__name__, static_folder=None)
    job = ScanJob()
    idx_job = IndexJob()
    local = threading.local()

    def conn():
        """Uma conexao por thread: sqlite3 nao permite compartilhar entre threads."""
        if getattr(local, "con", None) is None:
            local.con = db.init(db.connect())
        return local.con

    # ---- estatico ---------------------------------------------------------

    def _no_cache(response):
        """Tudo vem de 127.0.0.1: cachear nao economiza nada e faz uma versao
        nova do app.js continuar servindo a antiga depois de uma atualizacao."""
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    @app.route("/")
    def index():
        return _no_cache(send_from_directory(resource_dir(), "index.html"))

    @app.route("/<path:name>")
    def static_file(name):
        return _no_cache(send_from_directory(resource_dir(), name))

    # ---- leitura ----------------------------------------------------------

    @app.get("/api/bootstrap")
    def bootstrap():
        con = conn()
        cfg = settings.load()
        totals = {
            "projects": con.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "sessions": con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "artifacts": con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            "repos": con.execute("SELECT COUNT(*) FROM git_state WHERE has_git=1").fetchone()[0],
        }
        return jsonify(
            {
                "settings": cfg,
                "totals": totals,
                "last_scan": float(db.get_meta(con, "last_scan", 0) or 0),
                "scan": job.state,
                "git_available": gitops.git_available(),
            }
        )

    @app.get("/api/radar")
    def api_radar():
        con = conn()
        cfg = settings.load()
        snap = radar.snapshot(con, cfg.get("live_window_minutes", 30))
        return jsonify({"projects": _projects_payload(con, snap), "radar": snap})

    @app.get("/api/timeline")
    def api_timeline():
        con = conn()
        limit = min(int(request.args.get("limit", 100)), 500)
        offset = int(request.args.get("offset", 0))
        where, params = ["e.text IS NOT NULL"], []
        ai = request.args.get("ai")
        if ai and ai != "todas":
            where.append("e.ai = ?")
            params.append(ai)
        pid = request.args.get("project_id")
        if pid:
            where.append("e.project_id = ?")
            params.append(int(pid))
        since = request.args.get("since")
        if since:
            where.append("e.ts >= ?")
            params.append(float(since))
        rows = con.execute(
            "SELECT e.id, e.ai, e.ts, e.kind, e.text, e.project_id, e.session_id, "
            "       p.name AS project_name, s.title AS session_title, s.model "
            "FROM events e LEFT JOIN projects p ON p.id = e.project_id "
            "LEFT JOIN sessions s ON s.id = e.session_id "
            "WHERE %s ORDER BY e.ts DESC LIMIT ? OFFSET ?" % " AND ".join(where),
            params + [limit, offset],
        ).fetchall()
        return jsonify({"events": [dict(r) for r in rows], "offset": offset, "limit": limit})

    @app.get("/api/project/<int:pid>")
    def api_project(pid):
        project = _projeto_completo(conn(), pid)
        if project is None:
            return jsonify({"error": "projeto nao encontrado"}), 404
        return jsonify(project)

    def _projeto_completo(con, pid):
        """Payload do projeto. Uma funcao so', usada pela gaveta E pelo dossie --
        senao o dossie mostraria numeros diferentes da tela que o gerou."""
        row = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            return None
        project = dict(row)

        project["sessions"] = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM sessions WHERE project_id=? ORDER BY COALESCE(ended_at,started_at) DESC",
                (pid,),
            )
        ]
        git_row = con.execute("SELECT * FROM git_state WHERE project_id=?", (pid,)).fetchone()
        project["git"] = dict(git_row) if git_row else None
        if git_row and git_row["has_git"]:
            project["commits"] = gitops.recent_commits(row["path"], 40)
            project["dirty"] = gitops.dirty_files(row["path"])
        else:
            project["commits"], project["dirty"] = [], []

        project["artifacts"] = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM artifacts WHERE project_id=? ORDER BY kind, mtime DESC LIMIT 500",
                (pid,),
            )
        ]
        project["inline_media"] = [
            dict(r) for r in con.execute(
                "SELECT id, ai, origem, media_type, bytes, ts, session_id "
                "FROM inline_media WHERE project_id=? ORDER BY ts DESC LIMIT 400",
                (pid,),
            )
        ]
        project["context_files"] = _context_files(row["path"])
        project["cost"] = _cost_rows(con, pid)
        project["touched"] = _touched_rows(con, row["path"], project["dirty"])
        project["pastas"] = _pastas_ligadas(con, pid, row["path"])
        project["sinais"] = sinais.por_projeto(con, row["path"])
        # Metas saem do arquivo a cada abertura: sao cinco .md pequenos, e assim a
        # lista ja' reflete o que a IA escreveu ha' um minuto, sem sincronizacao.
        project["metas"] = metas.ler(row["path"], _metas_extras(con, pid))

        # So' o que e' barato entra aqui. A contagem de arquivos do preflight
        # leva 9s num projeto de 39 mil arquivos e fica no endpoint proprio,
        # chamado sob demanda antes de um init/snapshot.
        project["targets"] = actions.available_targets()          # ~20 ms
        project["snapshots"] = actions.list_snapshots(row["path"])
        project["sugestao"] = (
            actions.suggest_message(row["path"])                   # ~60 ms
            if git_row and git_row["has_git"] else None
        )
        return project

    @app.post("/api/project/<int:pid>/dossie")
    def api_dossie(pid):
        """Markdown com tudo que o painel sabe do projeto, para colar noutra IA.

        Grava em %LOCALAPPDATA%, fora do projeto -- por isso passa pelo Modo
        Seguro, como o snapshot.
        """
        def run():
            projeto = _projeto_completo(conn(), pid)
            if projeto is None:
                raise actions.ActionError("projeto nao encontrado")
            return dossie.salvar(projeto)
        return _run_action(run)

    @app.get("/api/search")
    def api_search():
        con = conn()
        query = (request.args.get("q") or "").strip()
        vazio = {"results": [], "query": query, "total": 0, "achados": 0,
                 "facetas": {"ai": {}, "kind": {}, "projetos": []}, "truncado": False}
        if len(query) < 2:
            return jsonify(vazio)
        limit = min(int(request.args.get("limit", 60)), 200)
        try:
            fts = _fts_query(query)
            # Teto generoso na consulta e filtro depois: o FTS5 nao sabe de IA nem
            # de projeto (o `ref` e' UNINDEXED), entao estreitar so' e' possivel
            # depois de resolver quem e' quem.
            rows = con.execute(
                "SELECT ref, title, snippet(search_fts, 0, '<<', '>>', ' ... ', 14) AS trecho, rank "
                "FROM search_fts WHERE search_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts, _BUSCA_TETO),
            ).fetchall()
            total = con.execute(
                "SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH ?", (fts,)
            ).fetchone()[0]
        except Exception as exc:
            return jsonify(dict(vazio, error=str(exc)))

        offset = max(0, int(request.args.get("offset", 0)))
        itens = _hydrate_search(con, rows)
        facetas = _facetas(itens)
        filtrados = _filtrar_busca(itens, request.args)
        agrupados = _agrupar_semelhantes(filtrados)
        return jsonify(
            {
                "results": agrupados[offset:offset + limit],
                "query": query,
                "total": total,                    # acertos no indice, sem filtro
                "brutos": len(filtrados),          # depois dos filtros, sem agrupar
                "achados": len(agrupados),         # grupos, que e' o que a lista mostra
                "offset": offset,
                "facetas": facetas,
                "truncado": total > len(rows),
                "teto": _BUSCA_TETO,
            }
        )

    @app.get("/api/transcript")
    def api_transcript():
        """Uma fatia da conversa. O sid vem por query porque contem ':'."""
        con = conn()
        sid = request.args.get("sid") or ""
        row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            return jsonify({"error": "sessao nao encontrada"}), 404

        # Conversa grande ainda nao indexada: em vez de segurar a resposta por
        # 41 segundos, devolve na hora o tamanho do servico e manda a interface
        # acompanhar /api/transcript/indexar. Abaixo do limiar nao vale a pena o
        # ida-e-volta -- indexar 30 MB leva menos que a animacao de abrir.
        falta = transcript.precisa_indexar(con, row)
        if falta > _INDEX_ASSINCRONO and request.args.get("esperar") != "1":
            idx_job.start(sid)
            return jsonify({
                "indexando": True, "bytes": falta,
                "session": {"id": row["id"], "ai": row["ai"], "title": row["title"],
                            "size_bytes": row["size_bytes"], "source_path": row["source_path"]},
            })
        try:
            at = request.args.get("at")
            data = transcript.page(
                con, row,
                offset=int(request.args.get("offset", 0)),
                limit=min(int(request.args.get("limit", 40)), 120),
                roles=[r for r in (request.args.get("roles") or "").split(",") if r] or None,
                # `at` chega de um resultado de busca: e' a posicao da mensagem
                # dentro do arquivo, e faz a pagina abrir NELA.
                at=int(at) if at not in (None, "") else None,
            )
        except OSError as exc:
            return jsonify({"error": "nao consegui ler o arquivo: %s" % exc}), 500
        data["session"] = {
            "id": row["id"], "ai": row["ai"], "title": row["title"],
            "model": row["model"], "started_at": row["started_at"],
            "ended_at": row["ended_at"], "source_path": row["source_path"],
            "size_bytes": row["size_bytes"],
        }
        return jsonify(data)

    @app.get("/api/transcript/indexar")
    def api_transcript_indexar():
        """Progresso da indexacao em curso. A interface pergunta a cada 400 ms."""
        return jsonify(idx_job.state)

    @app.post("/api/transcript/export")
    def api_export():
        con = conn()
        payload = request.get_json(silent=True) or {}
        row = con.execute("SELECT * FROM sessions WHERE id=?", (payload.get("sid") or "",)).fetchone()
        if row is None:
            return jsonify({"error": "sessao nao encontrada"}), 404

        def run():
            papeis = (list(transcript.TODOS) if payload.get("tudo")
                      else list(transcript.CONVERSA))
            return transcript.export_markdown(con, row, roles=papeis)
        return _run_action(run)

    @app.get("/api/custo")
    def api_custo():
        con = conn()
        agg = ("COUNT(*) n, SUM(tok_in) tok_in, SUM(tok_out) tok_out, "
               "SUM(tok_cache_r) tok_cache_r, SUM(tok_cache_w) tok_cache_w, "
               "SUM(tok_total) tok_total, SUM(sub_count) subs")
        return jsonify(
            {
                "por_ia": [
                    dict(r) for r in con.execute(
                        "SELECT ai, %s FROM sessions GROUP BY ai ORDER BY tok_total DESC" % agg
                    )
                ],
                "por_projeto": [
                    dict(r) for r in con.execute(
                        "SELECT s.project_id, p.name AS project_name, "
                        "  GROUP_CONCAT(DISTINCT s.ai) AS ais, %s "
                        "FROM sessions s LEFT JOIN projects p ON p.id = s.project_id "
                        "GROUP BY s.project_id ORDER BY tok_total DESC LIMIT 60" % agg
                    )
                ],
                "por_modelo": [
                    dict(r) for r in con.execute(
                        "SELECT ai, model, %s FROM sessions WHERE model IS NOT NULL "
                        "GROUP BY ai, model ORDER BY tok_total DESC" % agg
                    )
                ],
            }
        )

    @app.get("/api/graficos")
    def api_graficos():
        con = conn()
        cfg = settings.load()

        # --- Mapa de ritmo: dia da semana x hora, a partir das mensagens ---
        # Uma grade 7x24 em hora LOCAL. O SQLite ja' converte com 'localtime'.
        ritmo = [[0] * 24 for _ in range(7)]
        por_ia = {}
        for r in con.execute(
            "SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) AS dow, "
            "       CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hora, "
            "       ai, COUNT(*) n FROM events WHERE ts IS NOT NULL GROUP BY dow, hora, ai"
        ):
            # strftime %w: 0=domingo. A grade comeca na segunda.
            linha = (r["dow"] + 6) % 7
            ritmo[linha][r["hora"]] += r["n"]
            por_ia[r["ai"]] = por_ia.get(r["ai"], 0) + r["n"]

        # --- Consumo diario: EXATO, so' do Claude ---
        diario = [
            dict(r) for r in con.execute(
                "SELECT day, ai, SUM(tok_in) tok_in, SUM(tok_out) tok_out, "
                "  SUM(tok_cache_w) tok_cache_w, SUM(tok_cache_r) tok_cache_r, SUM(msgs) msgs "
                "FROM daily_usage GROUP BY day, ai ORDER BY day"
            )
        ]

        # --- Codex: so' existe o total por thread, sem quebra por dia. Vai
        # num grafico separado, com a data de encerramento, e rotulado como tal.
        threads = [
            dict(r) for r in con.execute(
                "SELECT s.id, s.title, s.model, s.tok_total, s.started_at, s.ended_at, "
                "  p.name AS project_name FROM sessions s "
                "LEFT JOIN projects p ON p.id = s.project_id "
                "WHERE s.ai='codex' AND s.tok_total > 0 ORDER BY s.ended_at"
            )
        ]

        return jsonify(
            {
                "ritmo": {"grade": ritmo, "por_ia": por_ia,
                          "total": sum(sum(l) for l in ritmo)},
                "diario": diario,
                "codex_threads": threads,
                "custo": _custo(con, cfg),
            }
        )

    @app.get("/api/faxina")
    def api_faxina():
        con = conn()
        parado = util.now() - 90 * 86400

        # Duplicata = mesmo nome de pasta aparecendo em raizes diferentes.
        grupos = {}
        for row in con.execute(
            "SELECT p.id, p.name, p.path, p.last_activity, p.fs_mtime, "
            "  (SELECT COUNT(*) FROM sessions s WHERE s.project_id=p.id) AS session_count "
            "FROM projects p WHERE p.on_disk=1"
        ):
            grupos.setdefault(row["name"].strip().lower(), []).append(dict(row))
        duplicatas = [
            {"name": copias[0]["name"], "copias": sorted(
                copias, key=lambda c: c.get("last_activity") or c.get("fs_mtime") or 0, reverse=True)}
            for copias in grupos.values() if len(copias) > 1
        ]
        duplicatas.sort(key=lambda g: -len(g["copias"]))

        # Trabalho de IA sem NENHUMA rede: nem git, nem snapshot. E' a pergunta
        # que o programa promete responder ("quais projetos nem repositorio
        # tem") e ate' agora so' aparecia como um selo neutro no card.
        tocados = _tocados_por_projeto(con)
        desprotegidos = []
        for r in con.execute(
            "SELECT p.id, p.name, p.path, p.last_activity, p.fs_mtime, "
            "  COALESCE(g.has_git, 0) AS has_git "
            "FROM projects p LEFT JOIN git_state g ON g.project_id=p.id "
            "WHERE p.on_disk=1 AND COALESCE(g.has_git, 0)=0"
        ):
            n = tocados.get(r["id"], 0)
            if not n:
                continue
            snaps = actions.list_snapshots(r["path"])
            desprotegidos.append(
                dict(r, arquivos=n, snapshots=len(snaps),
                     ultimo_snapshot=(snaps[0]["mtime"] if snaps else None))
            )
        desprotegidos.sort(key=lambda d: (d["snapshots"], -d["arquivos"]))

        return jsonify(
            {
                "desprotegidos": desprotegidos,
                "duplicatas": duplicatas,
                "parados": [
                    dict(r) for r in con.execute(
                        "SELECT id, name, path, last_activity, fs_mtime, size_bytes, file_count "
                        "FROM projects WHERE on_disk=1 AND COALESCE(last_activity, fs_mtime, 0) < ? "
                        "ORDER BY COALESCE(size_bytes, 0) DESC, COALESCE(last_activity, fs_mtime, 0) ASC "
                        "LIMIT 40",
                        (parado,),
                    )
                ],
                # Ordenado por peso: numa faxina o que decide e' quanto ocupa,
                # nao a ordem alfabetica.
                "pesados": [
                    dict(r) for r in con.execute(
                        "SELECT id, name, path, size_bytes, file_count, last_activity, fs_mtime "
                        "FROM projects WHERE on_disk=1 AND size_bytes IS NOT NULL "
                        "ORDER BY size_bytes DESC LIMIT 20"
                    )
                ],
                "medidos": con.execute(
                    "SELECT COUNT(*) FROM projects WHERE on_disk=1 AND size_bytes IS NOT NULL"
                ).fetchone()[0],
                "total_projetos": con.execute(
                    "SELECT COUNT(*) FROM projects WHERE on_disk=1"
                ).fetchone()[0],
                "disco": [
                    dict(r) for r in con.execute(
                        "SELECT ai, COUNT(*) n, SUM(size_bytes) bytes, MAX(size_bytes) maior "
                        "FROM sessions GROUP BY ai ORDER BY bytes DESC"
                    )
                ],
                "gigantes": [
                    dict(r) for r in con.execute(
                        "SELECT s.id, s.ai, s.native_id, s.title, s.first_prompt, s.size_bytes, "
                        "  s.ended_at, s.project_id, p.name AS project_name "
                        "FROM sessions s LEFT JOIN projects p ON p.id = s.project_id "
                        "WHERE s.size_bytes > 0 ORDER BY s.size_bytes DESC LIMIT 15"
                    )
                ],
            }
        )

    @app.get("/api/media/<int:mid>")
    def api_media(mid):
        """Serve uma imagem que vive dentro do JSONL.

        Le so' a linha (seek + read), decodifica o base64 na hora e devolve os
        bytes. Nada foi copiado para o disco na indexacao.
        """
        con = conn()
        row = con.execute("SELECT * FROM inline_media WHERE id=?", (mid,)).fetchone()
        if row is None:
            return jsonify({"error": "imagem nao encontrada"}), 404
        tipo, dados = media.ler(row)
        if dados is None:
            return jsonify({"error": "nao consegui decodificar a imagem"}), 404
        resp = app.response_class(dados, mimetype=tipo or "image/png")
        resp.headers["Cache-Control"] = "private, max-age=600"
        return resp

    @app.get("/api/artifact/<int:aid>")
    def api_artifact(aid):
        """Serve so' arquivos que estao na tabela artifacts -- lista branca."""
        con = conn()
        row = con.execute("SELECT path, name FROM artifacts WHERE id=?", (aid,)).fetchone()
        if row is None or not os.path.isfile(row["path"]):
            return jsonify({"error": "artefato nao encontrado"}), 404
        if request.args.get("raw"):
            return send_file(row["path"], download_name=row["name"])
        try:
            with open(row["path"], encoding="utf-8", errors="replace") as fh:
                return jsonify({"name": row["name"], "text": fh.read(400_000)})
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500

    # ---- escrita ----------------------------------------------------------

    # ---- acoes (Modo Seguro barra as que escrevem no projeto) --------------

    def _project_path(pid):
        row = conn().execute("SELECT path, name FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise actions.ActionError("projeto nao encontrado")
        return row["path"], row["name"]

    def _run_action(fn):
        """Traduz as excecoes das acoes em status HTTP corretos.

        403 no Modo Seguro e' importante: prova que o bloqueio vive no servidor,
        nao so' no botao desabilitado da interface.
        """
        try:
            return jsonify(fn())
        except actions.SafeModeError as exc:
            return jsonify({"error": str(exc), "safe_mode": True}), 403
        except actions.ActionError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "%s: %s" % (type(exc).__name__, exc)}), 500

    @app.get("/api/project/<int:pid>/preflight")
    def api_preflight(pid):
        def run():
            path, _ = _project_path(pid)
            info = actions.preflight(path)
            info["sugestao"] = actions.suggest_message(path) if info["has_git"] else None
            info["snapshots"] = actions.list_snapshots(path)
            info["targets"] = actions.available_targets()
            return info
        return _run_action(run)

    @app.post("/api/project/<int:pid>/git-init")
    def api_git_init(pid):
        payload = request.get_json(silent=True) or {}

        def run():
            path, _ = _project_path(pid)
            out = actions.git_init(
                path,
                write_gitignore=payload.get("gitignore", True),
                first_commit=payload.get("commit", True),
                message=payload.get("message"),
            )
            gitops.refresh(conn(), pid, path)
            conn().commit()
            return out
        return _run_action(run)

    @app.post("/api/project/<int:pid>/commit")
    def api_commit(pid):
        payload = request.get_json(silent=True) or {}

        def run():
            path, _ = _project_path(pid)
            out = actions.git_commit(path, payload.get("message"), payload.get("files"))
            gitops.refresh(conn(), pid, path)
            conn().commit()
            return out
        return _run_action(run)

    @app.post("/api/project/<int:pid>/snapshot")
    def api_snapshot(pid):
        payload = request.get_json(silent=True) or {}

        def run():
            path, name = _project_path(pid)
            apenas = payload.get("apenas")
            # "so' o que a IA tocou": a lista sai do indice, nao do cliente, para
            # que o pedido nao possa apontar para fora do projeto.
            if apenas == "ia":
                apenas = _tocados_relativos(conn(), path)
                if not apenas:
                    raise actions.ActionError("nenhum arquivo de IA registrado neste projeto")
            return actions.snapshot(path, name, payload.get("label"), apenas=apenas or None)
        return _run_action(run)

    @app.post("/api/project/<int:pid>/restore")
    def api_restore(pid):
        payload = request.get_json(silent=True) or {}

        def run():
            path, _ = _project_path(pid)
            out = actions.restore_snapshot(path, payload.get("zip") or "")
            gitops.refresh(conn(), pid, path)
            conn().commit()
            return out
        return _run_action(run)

    @app.post("/api/project/<int:pid>/open")
    def api_open(pid):
        payload = request.get_json(silent=True) or {}

        def run():
            path, _ = _project_path(pid)
            return actions.open_in(payload.get("target", "explorer"), path)
        return _run_action(run)

    @app.post("/api/scan")
    def api_scan():
        payload = request.get_json(silent=True) or {}
        started = job.start(
            measure=bool(payload.get("measure")), with_git=payload.get("git", True)
        )
        return jsonify({"started": started, "scan": job.state})

    @app.get("/api/scan")
    def api_scan_status():
        return jsonify(job.state)

    @app.get("/api/settings")
    def api_settings_get():
        return jsonify(settings.load())

    @app.post("/api/settings")
    def api_settings_post():
        return jsonify(settings.save(request.get_json(silent=True) or {}))

    return app, job


# ---- montagem dos payloads ------------------------------------------------

def _projects_payload(con, snap):
    live = snap["by_project"]
    since = util.now() - SPARK_DAYS * 86400

    spark = {}
    for row in con.execute(
        "SELECT project_id, CAST((? - ts) / 86400 AS INTEGER) AS dia, COUNT(*) n "
        "FROM events WHERE ts >= ? AND project_id IS NOT NULL GROUP BY project_id, dia",
        (util.now(), since),
    ):
        bucket = spark.setdefault(row["project_id"], [0] * SPARK_DAYS)
        idx = SPARK_DAYS - 1 - min(row["dia"], SPARK_DAYS - 1)
        bucket[idx] += row["n"]

    tocados = _tocados_por_projeto(con)

    rows = con.execute(
        "SELECT p.*, "
        "  (SELECT GROUP_CONCAT(DISTINCT s.ai) FROM sessions s WHERE s.project_id=p.id) AS ais, "
        "  (SELECT COUNT(*) FROM sessions s WHERE s.project_id=p.id) AS session_count, "
        "  (SELECT SUM(s.tok_total) FROM sessions s WHERE s.project_id=p.id) AS tok_total, "
        "  (SELECT s.model FROM sessions s WHERE s.project_id=p.id "
        "     ORDER BY COALESCE(s.ended_at,0) DESC LIMIT 1) AS last_model, "
        "  (SELECT s.ai FROM sessions s WHERE s.project_id=p.id "
        "     ORDER BY COALESCE(s.ended_at,0) DESC LIMIT 1) AS last_ai, "
        "  (SELECT s.title FROM sessions s WHERE s.project_id=p.id "
        "     ORDER BY COALESCE(s.ended_at,0) DESC LIMIT 1) AS last_title, "
        "  g.has_git, g.branch, g.commit_count, g.dirty_count, g.last_ts, g.last_msg, g.remote "
        "FROM projects p LEFT JOIN git_state g ON g.project_id = p.id "
        "ORDER BY COALESCE(p.last_activity, p.fs_mtime, 0) DESC"
    ).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        item["ais"] = sorted(set((row["ais"] or "").split(","))) if row["ais"] else []
        item["spark"] = spark.get(row["id"], [0] * SPARK_DAYS)
        item["live"] = live.get(str(row["id"]))
        item["arquivos_ia"] = tocados.get(row["id"], 0)
        item["snapshots"] = (
            len(actions.list_snapshots(row["path"])) if row["on_disk"] else 0
        )
        item["alerta"] = _alerta(row, item["arquivos_ia"], item["snapshots"])
        out.append(item)
    return out


def _tocados_por_projeto(con):
    """Quantos arquivos distintos a IA escreveu DENTRO de cada projeto.

    O casamento e' por caminho, com o prefixo mais longo vencendo -- a mesma
    regra do ProjectResolver. Nao da' para confiar no `project_id` da linha: ele
    diz de qual conversa veio a escrita, e a IA edita fora do projeto dela
    (memoria, configuracao, arquivo de outro projeto).

    Uma consulta e um casamento em memoria: 1.862 caminhos contra 47 projetos
    sai em milissegundos, contra 47 consultas com INSTR.
    """
    projetos = sorted(
        ((util.path_key(r["path"]) or "") + util.SEP, r["id"])
        for r in con.execute("SELECT id, path FROM projects")
    )
    projetos.sort(key=lambda kv: len(kv[0]), reverse=True)
    contagem = {}
    vistos = set()
    for (caminho,) in con.execute("SELECT DISTINCT path FROM touched_files"):
        chave = (caminho or "").lower()
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        for prefixo, pid in projetos:
            if len(prefixo) > 1 and chave.startswith(prefixo):
                contagem[pid] = contagem.get(pid, 0) + 1
                break
    return contagem


def _custo(con, cfg):
    """Estimativa em dinheiro a partir da tabela de precos do usuario.

    Os precos NAO vem embutidos com valores: inventar preco produz um numero de
    dinheiro confiantemente errado, que e' pior do que numero nenhum. A tabela
    chega preenchida com os modelos encontrados no indice e o valor zerado; ate'
    o usuario informar, a estimativa aparece como indisponivel.
    """
    precos = cfg.get("prices") or {}
    linhas, total = [], 0.0
    faltando = []

    # '<synthetic>' e' um marcador interno do Claude, nao um modelo cobravel.
    # Deixa-lo na lista fazia a interface pedir preco para algo que nao existe.
    for r in con.execute(
        "SELECT ai, model, COUNT(*) n, SUM(tok_in) tin, SUM(tok_out) tout, "
        "  SUM(tok_cache_w) tcw, SUM(tok_cache_r) tcr, SUM(tok_total) ttot "
        "FROM sessions WHERE model IS NOT NULL AND model NOT LIKE '<%' "
        "GROUP BY ai, model ORDER BY ttot DESC"
    ):
        p = precos.get(r["model"]) or {}
        tem_preco = any(float(p.get(k) or 0) > 0 for k in ("in", "out", "cache_w", "cache_r"))
        # Precos em dolares por MILHAO de tokens.
        valor = (
            (r["tin"] or 0) * float(p.get("in") or 0)
            + (r["tout"] or 0) * float(p.get("out") or 0)
            + (r["tcw"] or 0) * float(p.get("cache_w") or 0)
            + (r["tcr"] or 0) * float(p.get("cache_r") or 0)
        ) / 1_000_000.0
        if tem_preco:
            total += valor
        else:
            faltando.append(r["model"])
        linhas.append(
            {
                "ai": r["ai"], "model": r["model"], "n": r["n"],
                "tok_in": r["tin"], "tok_out": r["tout"],
                "tok_cache_w": r["tcw"], "tok_cache_r": r["tcr"],
                "tok_total": r["ttot"],
                "valor": round(valor, 2) if tem_preco else None,
                "sem_preco": not tem_preco,
                # O Codex nao separa entrada/saida; avisar em vez de fingir.
                "granularidade": "total" if r["ai"] == "codex" else "detalhada",
            }
        )

    return {
        "linhas": linhas,
        "total": round(total, 2),
        "moeda": cfg.get("currency", "USD"),
        "sem_preco": sorted(set(faltando)),
        "configurado": bool(precos) and not faltando,
    }


#: Teto de linhas devolvidas no payload da gaveta. As contagens do cabecalho sao
#: calculadas ANTES do corte, senao um projeto grande mentiria no proprio resumo.
#:
#: 1500 e' folgado de proposito: a aba ordena por peso, por data e por familia no
#: CLIENTE, e com um corte apertado o "mais pesado" seria apenas o mais pesado
#: ENTRE OS 400 primeiros por edicao -- uma ordenacao que mente. O maior projeto
#: medido tem 629 arquivos tocados; 1500 itens sao ~200 KB de JSON.
_TOCADOS_LISTA = 1500
_TOCADOS_TETO = 5000


def _tocados_relativos(con, raiz):
    """TODOS os caminhos relativos que a IA escreveu aqui, sem o teto de exibicao.

    A lista da gaveta e' cortada em 400 por ser interface; um snapshot nao pode
    proteger so' os 400 primeiros.
    """
    prefixo = (util.clean_path(raiz) or "").rstrip(util.SEP) + util.SEP
    if len(prefixo) < 4:
        return []
    return [
        r["path"][len(prefixo):]
        for r in con.execute(
            "SELECT DISTINCT path FROM touched_files WHERE INSTR(LOWER(path), LOWER(?)) = 1",
            (prefixo,),
        )
        if len(r["path"]) > len(prefixo)
    ]


def _metas_extras(con, pid):
    """Markdown do Antigravity (`brain/<id>/task.md`) que nao fica na raiz.

    Ele ja' esta' indexado em `artifacts` como contexto; so' precisamos apontar o
    parser para la'.
    """
    return [
        r["path"] for r in con.execute(
            "SELECT path FROM artifacts WHERE project_id=? AND kind='context' "
            "AND LOWER(name) IN ('task.md','implementation_plan.md','walkthrough.md') LIMIT 12",
            (pid,),
        )
    ]


def _pastas_ligadas(con, pid, raiz):
    """Onde este projeto realmente vive, segundo o indice -- sem varrer disco.

    Tres origens, e a terceira e' a que nao existia em lugar nenhum: arquivos que
    sessoes DESTE projeto editaram FORA dele. E' o que explica edicao que "some" --
    a IA mexeu na memoria, na configuracao, ou no projeto vizinho.
    """
    prefixo = (util.clean_path(raiz) or "").rstrip(util.SEP) + util.SEP
    dentro, fora = {}, {}
    for r in con.execute(
        "SELECT t.path, SUM(t.edits) e, MAX(t.last_ts) t, COUNT(DISTINCT t.session_id) s "
        "FROM touched_files t WHERE t.project_id = ? OR INSTR(LOWER(t.path), LOWER(?)) = 1 "
        "GROUP BY LOWER(t.path)",
        (pid, prefixo),
    ):
        caminho = r["path"]
        if caminho.lower().startswith(prefixo.lower()):
            rel = caminho[len(prefixo):]
            nome = rel.split(util.SEP)[0] if util.SEP in rel else "(raiz)"
            alvo, chave = dentro, nome
        else:
            alvo, chave = fora, util.clean_path(os.path.dirname(caminho)) or caminho
        b = alvo.setdefault(chave, {"nome": chave, "arquivos": 0, "escritas": 0, "last_ts": None})
        b["arquivos"] += 1
        b["escritas"] += r["e"] or 0
        b["last_ts"] = max(b["last_ts"] or 0, r["t"] or 0) or None

    # `cwd` das sessoes: uma conversa pode ter rodado numa subpasta do projeto.
    cwds = []
    for r in con.execute(
        "SELECT DISTINCT cwd FROM sessions WHERE project_id=? AND cwd IS NOT NULL", (pid,)
    ):
        limpo = util.clean_path(r["cwd"])
        if limpo and util.path_key(limpo) != util.path_key(raiz):
            cwds.append(limpo)

    ordenar = lambda d: sorted(d.values(), key=lambda b: -b["escritas"])
    return {
        "dentro": ordenar(dentro),
        "fora": ordenar(fora)[:20],
        "cwds": sorted(cwds)[:10],
        "raiz": util.clean_path(raiz),
    }


def _touched_rows(con, raiz, sujos):
    """Arquivos que a IA editou DENTRO deste projeto, cruzados com o git.

    O recorte e' por caminho, nao por sessao: um arquivo daqui conta mesmo que a
    conversa estivesse registrada em outro projeto, e um arquivo de fora (memoria,
    configuracao) nao polui a lista so' porque a sessao era deste projeto.
    """
    prefixo = (util.clean_path(raiz) or "").rstrip(util.SEP) + util.SEP
    if len(prefixo) < 4:
        return {"itens": [], "total": 0, "pendentes": 0, "novos": 0, "cortado": 0}
    linhas = con.execute(
        "SELECT path, GROUP_CONCAT(DISTINCT ai) ias, SUM(edits) edits, "
        "       MAX(last_ts) last_ts, COUNT(DISTINCT session_id) sessoes "
        "FROM touched_files WHERE INSTR(LOWER(path), LOWER(?)) = 1 "
        "GROUP BY LOWER(path) ORDER BY edits DESC, last_ts DESC LIMIT ?",
        (prefixo, _TOCADOS_TETO),
    ).fetchall()

    # O git devolve caminho relativo com barra normal; aqui tudo e' absoluto
    # com barra invertida. Normalizar dos dois lados antes de comparar.
    sujos_por_caminho = {
        (prefixo + f["path"].replace("/", util.SEP)).lower(): f["status"]
        for f in (sujos or [])
    }
    itens = []
    familias = {}
    for r in linhas:
        caminho = r["path"]
        # `stat` de uma vez, no mesmo laco que ja' checava a existencia -- medido:
        # 13 ms para 629 arquivos.
        st = util.stat_or_none(caminho)
        fam = touched.familia(caminho)
        familias[fam] = familias.get(fam, 0) + 1
        itens.append(
            {
                "path": caminho,
                "rel": caminho[len(prefixo):] if len(caminho) > len(prefixo) else caminho,
                "ias": [a for a in (r["ias"] or "").split(",") if a],
                "edits": r["edits"],
                "last_ts": r["last_ts"],
                "sessoes": r["sessoes"],
                "familia": fam,
                "bytes": st.st_size if st else None,
                "pendente": sujos_por_caminho.get(caminho.lower()),
                "sumiu": st is None,
            }
        )

    pendentes = [i for i in itens if i["pendente"]]
    novos = sum(1 for i in pendentes if (i["pendente"] or "").strip() == "??")
    # O que esta' pendente vai na frente: e' o motivo de alguem abrir esta aba, e
    # assim o corte da lista nunca esconde uma pendencia.
    itens.sort(key=lambda i: (0 if i["pendente"] else 1, -(i["edits"] or 0)))
    return {
        "itens": itens[:_TOCADOS_LISTA],
        "total": len(itens),
        "pendentes": len(pendentes),
        "novos": novos,
        "familias": familias,
        "bytes": sum(i["bytes"] or 0 for i in itens),
        "cortado": max(0, len(itens) - _TOCADOS_LISTA),
    }


def _cost_rows(con, pid):
    return [
        dict(r)
        for r in con.execute(
            "SELECT ai, model, COUNT(*) n, SUM(tok_in) tok_in, SUM(tok_out) tok_out, "
            "       SUM(tok_cache_r) tok_cache_r, SUM(tok_cache_w) tok_cache_w, "
            "       SUM(tok_total) tok_total, SUM(sub_count) subs "
            "FROM sessions WHERE project_id=? GROUP BY ai, model ORDER BY tok_total DESC",
            (pid,),
        )
    ]


CONTEXT_NAMES = (
    "CLAUDE.md", "AGENTS.md", "HANDOFF.md", "LEIA-ME.md", "README.md",
    "RETOMAR-AQUI.md", "GEMINI.md", ".cursorrules",
)


def _context_files(path):
    """Arquivos de contexto na raiz do projeto e um nivel do pai.

    O pai entra porque o usuario mantem RETOMAR-AQUI.md na pasta PAI dos projetos,
    fora de cada um deles.
    """
    found = []
    for base, escopo in ((path, "projeto"), (os.path.dirname(path or ""), "pasta pai")):
        if not base or not os.path.isdir(base):
            continue
        for name in CONTEXT_NAMES:
            full = os.path.join(base, name)
            st = util.stat_or_none(full)
            if st and os.path.isfile(full):
                found.append(
                    {"name": name, "path": full, "scope": escopo,
                     "size": st.st_size, "mtime": st.st_mtime}
                )
    return found


def _fts_query(raw):
    """Transforma texto livre em consulta FTS5 segura (aspas quebram a sintaxe)."""
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in raw).split() if t]
    if not terms:
        return '""'
    return " ".join('"%s"*' % t for t in terms)


#: Quantos acertos do FTS entram na peneira dos filtros. Acima disto a consulta
#: ja' e' generica demais para refinar, e o rotulo avisa que o total foi cortado.
_BUSCA_TETO = 1200


def _em_lotes(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _hydrate_search(con, rows):
    """Resolve os refs em LOTE -- tres consultas, nao uma por linha.

    Com o teto em 1.200 acertos, o caminho antigo (uma consulta por resultado)
    faria 1.200 idas ao banco so' para montar a lista da tela.
    """
    itens = []
    for row in rows:
        ref = row["ref"] or ""
        kind, _, ident = ref.partition(":")
        item = {"kind": kind, "title": row["title"], "snippet": row["trecho"], "ref": ref}
        if kind == "msg":
            # ref = "msg:<session_id>:<posicao>", e o session_id tem ':' dentro
            # ("claude:<uuid>") -- por isso a quebra e' pela ULTIMA.
            sid, _, posicao = ident.rpartition(":")
            item["session_id"] = sid
            item["at"] = posicao
            item["_chave"] = sid
        else:
            item["_chave"] = ident
        itens.append(item)

    def chaves(*tipos):
        return {i["_chave"] for i in itens if i["kind"] in tipos and i["_chave"]}

    sessoes = {}
    for lote in _em_lotes(chaves("session", "msg")):
        for r in con.execute(
            "SELECT s.id, s.ai, s.model, s.ended_at, s.project_id, s.title, "
            "p.name AS project_name FROM sessions s "
            "LEFT JOIN projects p ON p.id=s.project_id WHERE s.id IN (%s)"
            % ",".join("?" * len(lote)),
            lote,
        ):
            sessoes[r["id"]] = dict(r)

    eventos = {}
    ids = [int(c) for c in chaves("event") if c.isdigit()]
    for lote in _em_lotes(ids):
        for r in con.execute(
            "SELECT e.id, e.ai, e.ts AS ended_at, e.project_id, e.session_id, "
            "p.name AS project_name FROM events e "
            "LEFT JOIN projects p ON p.id=e.project_id WHERE e.id IN (%s)"
            % ",".join("?" * len(lote)),
            lote,
        ):
            eventos[r["id"]] = dict(r)

    artefatos = {}
    ids = [int(c) for c in chaves("artifact") if c.isdigit()]
    for lote in _em_lotes(ids):
        for r in con.execute(
            "SELECT a.id AS artifact_id, a.ai, a.mtime AS ended_at, a.project_id, a.path, "
            "p.name AS project_name FROM artifacts a "
            "LEFT JOIN projects p ON p.id=a.project_id WHERE a.id IN (%s)"
            % ",".join("?" * len(lote)),
            lote,
        ):
            artefatos[r["artifact_id"]] = dict(r)

    for item in itens:
        chave = item.pop("_chave", None)
        if item["kind"] in ("session", "msg"):
            s = sessoes.get(chave)
            if s:
                item.update(
                    ai=s["ai"], model=s["model"], ended_at=s["ended_at"],
                    project_id=s["project_id"], project_name=s["project_name"],
                    session_id=s["id"],
                )
                item["title"] = item["title"] or s["title"] or s["project_name"]
        elif item["kind"] == "event":
            e = eventos.get(int(chave)) if chave and chave.isdigit() else None
            if e:
                item.update({k: v for k, v in e.items() if k != "id"})
        elif item["kind"] == "artifact":
            a = artefatos.get(int(chave)) if chave and chave.isdigit() else None
            if a:
                item.update(a)
    return itens


def _facetas(itens):
    """O que existe entre os acertos, para cada pastilha dizer quanto vale."""
    por_ai, por_kind, por_projeto = {}, {}, {}
    for i in itens:
        ai = i.get("ai")
        if ai:
            por_ai[ai] = por_ai.get(ai, 0) + 1
        por_kind[i["kind"]] = por_kind.get(i["kind"], 0) + 1
        pid = i.get("project_id")
        if pid:
            atual = por_projeto.setdefault(
                pid, {"id": pid, "name": i.get("project_name") or ("projeto %d" % pid), "n": 0}
            )
            atual["n"] += 1
    return {
        "ai": por_ai,
        "kind": por_kind,
        "projetos": sorted(por_projeto.values(), key=lambda p: -p["n"])[:30],
    }


#: Quanto do trecho entra na chave de semelhanca. Curto demais junta coisa
#: diferente; longo demais nunca junta nada.
_SEMELHANTE_CHARS = 60

_SO_LETRAS = re.compile(r"[^a-zà-ÿ ]+")


def _chave_semelhanca(item):
    """Assinatura do trecho, para juntar resultados que sao a mesma coisa.

    Buscar `erro` trazia tres linhas praticamente identicas do mesmo log --
    `PHONE_REGISTRATION_ERROR [26716:27688:0823/022421.519:ERROR:...]` --, que
    so' diferem nos numeros de processo e no horario. Descartando digito e
    pontuacao, as tres colapsam numa assinatura so'.
    """
    texto = (item.get("snippet") or "").replace("<<", "").replace(">>", "")
    texto = _SO_LETRAS.sub(" ", texto.lower())
    return " ".join(texto.split())[:_SEMELHANTE_CHARS]


def _agrupar_semelhantes(itens):
    """Junta resultados quase iguais, preservando a ordem de relevancia.

    Criterio ESTREITO de proposito -- projeto, tipo e assinatura tem que bater
    exatamente. Esconder um resultado diferente e' pior do que repetir um igual,
    entao na duvida nao agrupa. O representante e' o primeiro (mais relevante) e
    leva os demais em `semelhantes`.
    """
    grupos = {}
    saida = []
    for item in itens:
        chave = (item.get("project_id"), item["kind"], _chave_semelhanca(item))
        if not chave[2]:
            saida.append(item)          # sem texto para comparar: nao agrupa
            continue
        chefe = grupos.get(chave)
        if chefe is None:
            grupos[chave] = item
            item["semelhantes"] = []
            saida.append(item)
        else:
            chefe["semelhantes"].append(item)
    return saida


def _filtrar_busca(itens, args):
    """Peneira em Python: o FTS5 nao sabe de IA, projeto nem data."""
    ai = args.get("ai")
    kind = args.get("kind")
    pid = args.get("project_id")
    dias = args.get("dias")
    corte = util.now() - int(dias) * 86400 if (dias or "").isdigit() and int(dias) > 0 else None
    saida = []
    for i in itens:
        if ai and ai != "todas" and i.get("ai") != ai:
            continue
        if kind and kind != "tudo" and i["kind"] != kind:
            continue
        if pid and str(i.get("project_id") or "") != str(pid):
            continue
        if corte and (i.get("ended_at") or 0) < corte:
            continue
        saida.append(i)
    return saida


#: Abaixo disto nao vira selo. Um arquivo solto nao e' exposicao, e o selo neutro
#: "sem git" ao lado ja' diz que falta repositorio. A Faxina lista todos, sem
#: piso -- la' a pessoa foi procurar; aqui o aviso foi ate' ela.
_MIN_ARQUIVOS_ALERTA = 5


def _alerta(row, arquivos_ia=0, snapshots=0):
    """Trabalho de IA sem rede de seguranca. Dois casos, mutuamente exclusivos.

    "57 arquivos sem commit" nao diz se e' de hoje ou de marco. O que importa e'
    a comparacao: a IA mexeu DEPOIS do ultimo commit?

    A primeira versao tambem alertava "projeto sem git" e pintava 41 dos 44
    cards -- aviso que aparece em 93% da tela nao avisa nada. Por isso o caso
    sem git tinha sido removido inteiro, e com ele foi embora justamente o pior
    cenario: 14 projetos onde a IA escreveu 1.088 arquivos sem git NEM snapshot.
    O selo neutro "sem git" dizia que faltava repositorio, nunca que havia
    trabalho de IA dentro dele.

    Agora o corte e' por EVIDENCIA, nao por ausencia: so' alerta onde a IA
    realmente escreveu (`touched_files`). Isso leva de 44 cards para 14.
    """
    atividade = row["last_activity"]

    if not row["has_git"]:
        if row["on_disk"] and arquivos_ia >= _MIN_ARQUIVOS_ALERTA:
            return {
                "tipo": "sem-rede" if not snapshots else "sem-git-com-snapshot",
                "arquivos": arquivos_ia,
                "texto": "%d arquivo%s escrito%s por IA, sem versionamento"
                         % (arquivos_ia, "s" if arquivos_ia > 1 else "",
                            "s" if arquivos_ia > 1 else "")
                         + ("" if not snapshots else " (ha snapshot)"),
            }
        return None

    if atividade and row["last_ts"] and atividade > row["last_ts"]:
        dias = int((atividade - row["last_ts"]) / 86400)
        return {
            "tipo": "nao-commitado",
            "dias": dias,
            "texto": "IA trabalhou depois do ultimo commit"
                     + (" (ha %d dias)" % dias if dias >= 1 else ""),
        }
    return None
