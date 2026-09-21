"""Orquestrador da indexacao.

Ordem: raizes -> projetos -> coletores das 3 IAs -> git -> busca.

Uso pela linha de comando (verificacao da Fase 1, sem interface):
    python -m core.scanner --report
    python -m core.scanner --report --reset     # apaga o indice e refaz do zero
    python -m core.scanner --report --measure   # mede tamanho em disco (lento)
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import db, gitops, indexer, settings, sinais, touched, transcript, util
from .collectors import antigravity as c_antigravity
from .collectors import claude as c_claude
from .collectors import codex as c_codex

COLLECTORS = (("claude", c_claude), ("codex", c_codex), ("antigravity", c_antigravity))

# Um diretorio so' conta como projeto se tiver algum destes sinais.
PROJECT_MARKERS = indexer.MARCAS_DE_PROJETO


def scan_roots(con, cfg, report):
    """Cada subpasta direta de uma raiz vira um projeto."""
    ignore = {n.lower() for n in cfg.get("ignore_names") or []}
    seen = set()
    for root in cfg.get("roots") or []:
        if not os.path.isdir(root):
            report.warn("raiz inexistente: " + root)
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError as exc:
            report.warn("raiz ilegivel %s (%s)" % (root, exc))
            continue
        for name in entries:
            if name.lower() in ignore or name.startswith("."):
                continue
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            _upsert_project(con, full, name, root, report)
            seen.add(util.path_key(full))
            report.bump("projetos_em_raiz")
    con.commit()
    return seen


def _shallow_mtime(path):
    """mtime mais recente entre a pasta e seus filhos diretos: barato e util.

    Um os.walk completo aqui seria inviavel -- ha' projeto com 18 mil arquivos.
    """
    newest = 0.0
    st = util.stat_or_none(path)
    if st:
        newest = st.st_mtime
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest or None


def _upsert_project(con, path, name, root, report):
    key = util.path_key(path)
    clean = util.clean_path(path)
    fs_mtime = _shallow_mtime(path)
    row = con.execute("SELECT id FROM projects WHERE key=?", (key,)).fetchone()
    if row:
        con.execute(
            "UPDATE projects SET path=?, name=?, root=?, on_disk=1, discovered=0, fs_mtime=? "
            "WHERE id=?",
            (clean, name, util.clean_path(root), fs_mtime, row["id"]),
        )
        return row["id"]
    cur = con.execute(
        "INSERT INTO projects(key, path, name, root, on_disk, discovered, first_seen, fs_mtime) "
        "VALUES(?,?,?,?,1,0,?,?)",
        (key, clean, name, util.clean_path(root), util.now(), fs_mtime),
    )
    report.bump("projetos_novos")
    return cur.lastrowid


def mark_missing(con):
    """Projeto que sumiu do disco vira on_disk=0 em vez de ser apagado."""
    for row in con.execute("SELECT id, path FROM projects").fetchall():
        exists = 1 if os.path.isdir(row["path"]) else 0
        con.execute("UPDATE projects SET on_disk=? WHERE id=?", (exists, row["id"]))
    con.commit()


def refresh_git(con, report, progress=None):
    if not gitops.git_available():
        # Preservar e' melhor que sobrescrever: sem o executavel do git, gravar
        # o estado "com erro" apagaria branch, commits e arquivos pendentes que
        # ja' estavam corretos no indice.
        report.warn("git nao encontrado no PATH -- estado dos repositorios preservado")
        db.set_meta(con, "git_missing", 1)
        return
    db.set_meta(con, "git_missing", 0)
    rows = con.execute(
        "SELECT id, path FROM projects WHERE on_disk=1 ORDER BY last_activity DESC"
    ).fetchall()
    total = max(len(rows), 1)
    for i, row in enumerate(rows):
        if progress:
            progress(i, total, "Git: " + util.basename(row["path"]))
        info = gitops.refresh(con, row["id"], row["path"])
        if info["has_git"]:
            report.bump("repos_git")
            if info["error"]:
                report.warn("git %s: %s" % (row["path"], info["error"]))
            elif info["dirty_count"]:
                report.bump("repos_sujos")
            _index_commits(con, report, row["id"], row["path"])
        else:
            report.bump("projetos_sem_git")
        if i % 10 == 0:
            con.commit()
    con.commit()


#: Quantos commits trazer da primeira vez que um repositorio e' visto. Depois
#: disso a leitura e' por data e traz so' o que apareceu desde o ultimo.
_COMMITS_PRIMEIRA_VEZ = 500


def _index_commits(con, report, project_id, path):
    """Commits viram eventos, ao lado dos prompts.

    O schema ja' previa kind='commit' desde o inicio e nada nunca gravava: a
    linha do tempo mostrava voce pedindo e sumia no exato momento em que o
    trabalho virava versao. O uid deduplica, entao reprocessar e' de graca.
    """
    ultimo = con.execute(
        "SELECT MAX(ts) FROM events WHERE project_id=? AND kind='commit'", (project_id,)
    ).fetchone()[0]
    if ultimo:
        commits = gitops.recent_commits(path, 200, since=ultimo)
    else:
        commits = gitops.recent_commits(path, _COMMITS_PRIMEIRA_VEZ)
    novos = 0
    for c in commits:
        texto = c["message"]
        if c["author"]:
            texto = "%s — %s" % (c["message"], c["author"])
        if indexer.add_event(
            con, "git:%d:%s" % (project_id, c["sha"]), "git", c["ts"], "commit",
            util.clip(texto, 500), project_id=project_id,
        ):
            novos += 1
    if novos:
        report.bump("commits", novos)


def refresh_search(con, report):
    """Alimenta o FTS5. Sessoes sao poucas e vao inteiras; eventos e artefatos
    entram por marca d'agua para nao reindexar tudo a cada varredura."""
    for row in con.execute(
        "SELECT id, title, first_prompt, cwd FROM sessions"
    ).fetchall():
        body = " ".join(filter(None, (row["first_prompt"], row["cwd"])))
        db.fts_replace(con, "session:" + row["id"], body, row["title"])
        report.bump("fts_sessoes")

    mark = int(db.get_meta(con, "fts_event_mark", 0) or 0)
    top = mark
    for row in con.execute(
        "SELECT id, text, kind FROM events WHERE id > ? ORDER BY id", (mark,)
    ).fetchall():
        if row["text"]:
            db.fts_replace(con, "event:%d" % row["id"], row["text"], None)
            report.bump("fts_eventos")
        top = max(top, row["id"])
    db.set_meta(con, "fts_event_mark", top)

    mark = int(db.get_meta(con, "fts_artifact_mark", 0) or 0)
    top = mark
    for row in con.execute(
        "SELECT id, path, name, size FROM artifacts WHERE kind='context' AND id > ? ORDER BY id",
        (mark,),
    ).fetchall():
        top = max(top, row["id"])
        if (row["size"] or 0) > 400_000:
            continue
        try:
            with open(row["path"], encoding="utf-8", errors="replace") as fh:
                text = fh.read(400_000)
        except OSError:
            continue
        db.fts_replace(con, "artifact:%d" % row["id"], text, row["name"])
        report.bump("fts_contextos")
    db.set_meta(con, "fts_artifact_mark", top)
    con.commit()


#: Versao dos dados DERIVADOS (arquivos tocados, busca nas respostas). Subir
#: este numero refaz a passada de backfill uma vez, na proxima varredura.
#:   1  primeira versao
#:   2  corrige o caminho do patch do Codex (regex gulosa + barra dobrada pela
#:      string de JS gravavam "HANDOFF.md\n@@\n  response..." como caminho)
DERIVADOS_V = 2

#: Versao da LIMPEZA de projetos adotados por engano. Subir roda uma vez.
#:   1  a pasta do usuario, adotada porque uma IA rodou dentro dela
#:   2  as areas de rascunho das ferramentas (Documents\Codex\<data>\<slug>,
#:      scratch do Claude, .chatgpt-projects) -- ver indexer.pode_adotar
LIMPEZA_V = 2


def limpar_falsos_projetos(con, report):
    """Devolve para "sem projeto" o que nunca deveria ter virado projeto.

    Usa exatamente o mesmo `indexer.pode_adotar()` da adocao, para que nao exista
    um projeto que o indice manteria mas nao criaria de novo -- um criterio so',
    aplicado dos dois lados.

    Projetos DESCOBERTOS apenas: o que veio de uma raiz configurada e' escolha da
    pessoa e nao cabe a nos desfazer.
    """
    proibidas = indexer.nunca_projeto()
    roots = settings.load().get("roots") or []
    alvos = [
        r["id"] for r in con.execute("SELECT id, path, discovered FROM projects")
        if r["discovered"] and not indexer.pode_adotar(r["path"], proibidas, roots)
    ]
    if not alvos:
        return
    marcadores = ",".join("?" * len(alvos))
    for tabela in ("sessions", "events", "touched_files", "inline_media", "artifacts"):
        con.execute(
            "UPDATE %s SET project_id=NULL WHERE project_id IN (%s)" % (tabela, marcadores),
            alvos,
        )
    con.execute("DELETE FROM git_state WHERE project_id IN (%s)" % marcadores, alvos)
    con.execute("DELETE FROM daily_usage WHERE project_id IN (%s)" % marcadores, alvos)
    con.execute("DELETE FROM projects WHERE id IN (%s)" % marcadores, alvos)
    con.commit()
    report.bump("projetos_falsos_removidos", len(alvos))


#: Versao da RECONTAGEM do Claude. Subir refaz a contagem de tokens uma vez.
#:   1  conserta o consumo dobrado: releitura do zero somava por cima do que ja'
#:      estava contado (medido: 428.232.892 no indice contra 221.103.397 reais).
RECONTAGEM_V = 1

_ASSIST_CLAUDE = b'"assistant"'
_ASSIST_CODEX = b'"role":"assistant"'
_MAX_LINHA = 8 * 1024 * 1024
_MAX_RESPOSTA = 512 * 1024


def refill_derivados(con, report, progress=None):
    """Preenche o que se EXTRAI das conversas ja' indexadas: arquivos tocados e
    texto de resposta para a busca.

    Por que uma passada separada, e nao "zerar last_offset e deixar o coletor
    reler"? Porque a leitura corrente e' incremental de proposito: merge_session()
    SOMA os numericos e add_daily_usage() soma sempre. Uma releitura pelo caminho
    normal dobraria todos os tokens do Claude -- 1,26 bi viraria 2,52 bi. Esta
    funcao le' os mesmos arquivos mas so' escreve em touched_files e no FTS,
    onde reescrever e' idempotente.

    Roda uma vez, guardada por DERIVADOS_V. Sessoes que o coletor acabou de ler
    do byte zero ficam de fora (report.completas): num indice novo o coletor ja'
    extraiu tudo, e sem essa poda os 5,1 GB seriam lidos DUAS vezes na mesma
    varredura -- o que, com o disco frio, custa minutos e nao segundos.
    """
    rows = con.execute(
        "SELECT id, ai, project_id, source_path FROM sessions "
        "WHERE ai IN ('claude','codex') AND source_path IS NOT NULL "
        "ORDER BY COALESCE(ended_at, 0) DESC"
    ).fetchall()
    rows = [r for r in rows if r["id"] not in report.completas]
    total = max(len(rows), 1)
    for i, row in enumerate(rows):
        if progress:
            progress(i, total, "Extraindo: " + util.basename(row["source_path"]))
        if not os.path.isfile(row["source_path"]):
            continue
        try:
            if row["ai"] == "claude":
                baldes, falas = _derivar_claude(row["source_path"])
            else:
                baldes, falas = _derivar_codex(row["source_path"])
        except OSError as exc:
            report.warn("derivados %s: %s" % (util.basename(row["source_path"]), exc))
            continue

        # substituir=True: esta leitura viu o arquivo INTEIRO, entao ela e'
        # autoridade sobre o total -- e nao soma em cima do que o coletor ja'
        # tenha posto nesta mesma varredura.
        report.bump(
            "derivados_arquivos",
            touched.gravar(con, row["id"], row["project_id"], row["ai"], baldes, substituir=True),
        )
        for offset, texto in falas:
            db.fts_replace(con, "msg:%s:%d" % (row["id"], offset), texto)
        report.bump("derivados_respostas", len(falas))
        con.commit()
    if progress:
        progress(total, total, "Extraindo: concluido")


def _derivar_claude(path):
    baldes, falas = {}, []
    consumed = 0
    with open(path, "rb") as fh:
        for raw in fh:
            inicio = consumed
            consumed += len(raw)
            if len(raw) > _MAX_LINHA:
                continue
            quer_tool = touched.MARCA_CLAUDE in raw
            quer_resp = _ASSIST_CLAUDE in raw and len(raw) <= _MAX_RESPOSTA
            if not (quer_tool or quer_resp):
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if quer_tool:
                touched.acumular(baldes, touched.de_claude(rec), util.to_epoch(rec.get("timestamp")))
            if quer_resp:
                fala = transcript.texto_de_resposta("claude", rec)
                if fala:
                    falas.append((inicio, fala[:transcript.LIMITE_BUSCA]))
    return baldes, falas


def _derivar_codex(path):
    baldes, falas = {}, []
    consumed = 0
    with open(path, "rb") as fh:
        for raw in fh:
            inicio = consumed
            consumed += len(raw)
            if len(raw) > _MAX_RESPOSTA:
                continue    # linha grande e' historico injetado, nunca patch nem fala
            quer_tool = touched.MARCA_CODEX in raw or touched.MARCA_CODEX_ALT in raw
            quer_resp = _ASSIST_CODEX in raw
            if not (quer_tool or quer_resp):
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if quer_tool:
                touched.acumular(baldes, touched.de_codex(rec), util.to_epoch(rec.get("timestamp")))
            if quer_resp:
                fala = transcript.texto_de_resposta("codex", rec)
                if fala:
                    falas.append((inicio, fala[:transcript.LIMITE_BUSCA]))
    return baldes, falas


def measure_projects(con, report, progress=None):
    """Tamanho em disco -- passada separada porque e' cara (ha' projeto de 20 GB)."""
    rows = con.execute("SELECT id, path FROM projects WHERE on_disk=1").fetchall()
    total = max(len(rows), 1)
    for i, row in enumerate(rows):
        if progress:
            progress(i, total, "Medindo: " + util.basename(row["path"]))
        size = count = 0
        for dirpath, dirs, files in os.walk(row["path"]):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__")]
            for name in files:
                st = util.stat_or_none(os.path.join(dirpath, name))
                if st:
                    size += st.st_size
                    count += 1
        con.execute(
            "UPDATE projects SET size_bytes=?, file_count=? WHERE id=?", (size, count, row["id"])
        )
        con.commit()
        report.bump("projetos_medidos")


def full_scan(con, cfg=None, report=None, progress=None, with_git=True, measure=False):
    cfg = cfg or settings.load()
    report = report or indexer.Report()
    t0 = time.time()

    # Uma versao nova dos derivados exige reler as conversas. O Antigravity nao
    # tem leitura incremental (o blob e' lido inteiro sempre), entao para ele
    # basta esquecer o estado das fontes: o coletor refaz tudo, e refazer la' e'
    # idempotente porque merge_session usa absolute=True.
    # Consumo do Claude contado em dobro: apagar o estado das fontes faz o
    # coletor reler do zero, e agora ler do zero zera a sessao antes (ver
    # claude._esquecer). Uma vez so'; custa a releitura de 784 MB de JSONL.
    if int(db.get_meta(con, "recontagem_v", 0) or 0) < RECONTAGEM_V:
        con.execute(
            "DELETE FROM source_files WHERE kind IN ('claude_jsonl','claude_subagent')"
        )
        con.commit()
        report.log("recontando o consumo do Claude (uma vez)")
        _recontar = True
    else:
        _recontar = False

    derivados_pendentes = int(db.get_meta(con, "derivados_v", 0) or 0) < DERIVADOS_V
    if derivados_pendentes:
        con.execute("DELETE FROM source_files WHERE kind=?", (c_antigravity.KIND,))
        # A busca e' refeita do zero porque o rowid dela passou a ser derivado do
        # ref (ver db.fts_rowid): linhas antigas, com rowid automatico, nao seriam
        # encontradas na hora de reescrever e virariam duplicata. Zerar as marcas
        # d'agua faz eventos e artefatos entrarem de novo; as respostas voltam
        # pelos coletores e por refill_derivados, que rodam nesta mesma varredura.
        con.execute("DELETE FROM search_fts")
        db.set_meta(con, "fts_event_mark", 0)
        db.set_meta(con, "fts_artifact_mark", 0)
        con.commit()

    if int(db.get_meta(con, "limpeza_v", 0) or 0) < LIMPEZA_V:
        limpar_falsos_projetos(con, report)
        db.set_meta(con, "limpeza_v", LIMPEZA_V)
        con.commit()

    scan_roots(con, cfg, report)
    report.timings["raizes"] = time.time() - t0

    resolver = indexer.ProjectResolver(con, adopt_discovered=cfg.get("adopt_discovered", True))
    for label, module in COLLECTORS:
        t = time.time()
        try:
            module.collect(con, resolver, report, progress)
        except Exception as exc:
            report.warn("coletor %s falhou: %s" % (label, exc))
        report.timings[label] = time.time() - t
        resolver.reload()

    indexer.refresh_project_activity(con)
    mark_missing(con)

    if with_git:
        t = time.time()
        refresh_git(con, report, progress)
        report.timings["git"] = time.time() - t

    if _recontar:
        db.set_meta(con, "recontagem_v", RECONTAGEM_V)
        con.commit()

    if derivados_pendentes:
        t = time.time()
        refill_derivados(con, report, progress)
        db.set_meta(con, "derivados_v", DERIVADOS_V)
        con.commit()
        report.timings["derivados"] = time.time() - t

    # Depois dos coletores, porque le' `touched_files`: sem eles a lista de
    # arquivos da IA ainda nao existe.
    t = time.time()
    sinais.varrer(con, report, progress)
    report.timings["sinais"] = time.time() - t

    t = time.time()
    refresh_search(con, report)
    report.timings["busca"] = time.time() - t

    if measure:
        t = time.time()
        measure_projects(con, report, progress)
        report.timings["medicao"] = time.time() - t

    db.set_meta(con, "last_scan", util.now())
    con.commit()
    report.timings["total"] = time.time() - t0
    return report


# ---- CLI de verificacao ---------------------------------------------------

def _print_report(con, report):
    print("\n=== CONTAGENS ===")
    for key in sorted(report.counts):
        print("  %-28s %s" % (key, report.counts[key]))

    print("\n=== TEMPOS ===")
    for key, value in report.timings.items():
        print("  %-28s %.2fs" % (key, value))

    print("\n=== PROJETOS POR IA ===")
    rows = con.execute(
        "SELECT p.name, p.path, p.discovered, "
        "  GROUP_CONCAT(DISTINCT s.ai) AS ias, COUNT(s.id) AS n, "
        "  SUM(s.tok_total) AS toks, MAX(s.ended_at) AS last, "
        "  g.has_git, g.branch, g.dirty_count, g.commit_count "
        "FROM projects p LEFT JOIN sessions s ON s.project_id = p.id "
        "LEFT JOIN git_state g ON g.project_id = p.id "
        "GROUP BY p.id ORDER BY last DESC NULLS LAST, p.name"
    ).fetchall()
    print("  %-26s %-22s %4s %10s %-11s %s" % ("PROJETO", "IAs", "SESS", "TOKENS", "GIT", "ULTIMA"))
    for r in rows:
        if not r["ias"] and not r["has_git"]:
            continue
        git = "-"
        if r["has_git"]:
            git = "%s/%s%s" % (
                r["branch"] or "?", r["commit_count"] if r["commit_count"] is not None else "?",
                (" +%d" % r["dirty_count"]) if r["dirty_count"] else "",
            )
        last = time.strftime("%Y-%m-%d", time.localtime(r["last"])) if r["last"] else "-"
        toks = "%.1fM" % (r["toks"] / 1e6) if r["toks"] else "-"
        name = (r["name"] or "")[:26] + ("*" if r["discovered"] else "")
        print("  %-26s %-22s %4d %10s %-11s %s" % (name[:26], (r["ias"] or "-")[:22], r["n"], toks, git, last))
    print("  (* = descoberto pelo cwd de uma IA, fora das raizes configuradas)")

    print("\n=== TOTAIS ===")
    for label, sql in (
        ("projetos", "SELECT COUNT(*) FROM projects"),
        ("  em raiz", "SELECT COUNT(*) FROM projects WHERE discovered=0"),
        ("  no disco", "SELECT COUNT(*) FROM projects WHERE on_disk=1"),
        ("sessoes", "SELECT COUNT(*) FROM sessions"),
        ("eventos", "SELECT COUNT(*) FROM events"),
        ("artefatos", "SELECT COUNT(*) FROM artifacts"),
        ("repos git", "SELECT COUNT(*) FROM git_state WHERE has_git=1"),
    ):
        print("  %-12s %s" % (label, con.execute(sql).fetchone()[0]))

    print("\n=== SESSOES POR IA ===")
    for r in con.execute(
        "SELECT ai, COUNT(*) n, SUM(tok_total) t, SUM(size_bytes) b, MAX(ended_at) last "
        "FROM sessions GROUP BY ai ORDER BY n DESC"
    ):
        print(
            "  %-12s %4d sessoes  %12s tokens  %10s  ultima %s"
            % (
                r["ai"], r["n"], "%.1fM" % ((r["t"] or 0) / 1e6), util.human_size(r["b"] or 0),
                time.strftime("%Y-%m-%d", time.localtime(r["last"])) if r["last"] else "-",
            )
        )

    if report.warnings:
        print("\n=== AVISOS (%d) ===" % len(report.warnings))
        for w in report.warnings[:20]:
            print("  - " + w)
        if len(report.warnings) > 20:
            print("  ... e mais %d" % (len(report.warnings) - 20))


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="Indexador do ProjectCommit")
    ap.add_argument("--report", action="store_true", help="imprime o relatorio ao final")
    ap.add_argument("--reset", action="store_true", help="apaga o indice antes de varrer")
    ap.add_argument("--measure", action="store_true", help="mede tamanho em disco (lento)")
    ap.add_argument("--no-git", action="store_true", help="pula a leitura do git")
    ap.add_argument("--quiet", action="store_true", help="sem barra de progresso")
    args = ap.parse_args(argv)

    if args.reset:
        # No Windows um arquivo aberto por outro processo NAO pode ser removido.
        # Antes isto falhava em silencio e imprimia "indice apagado" mesmo assim:
        # a varredura seguinte reaproveitava source_files, terminava em 2s e
        # parecia uma reindexacao completa relampago. Agora falha alto.
        falhas = []
        for suffix in ("", "-wal", "-shm"):
            alvo = settings.index_path() + suffix
            if not os.path.exists(alvo):
                continue
            try:
                os.remove(alvo)
            except OSError as exc:
                falhas.append("%s (%s)" % (os.path.basename(alvo), exc.strerror or exc))
        if falhas:
            print("ERRO: nao consegui apagar o indice: " + "; ".join(falhas), file=sys.stderr)
            print("      feche o ProjectCommit (janela ou --server) e tente de novo.", file=sys.stderr)
            return 2
        print("indice apagado")

    con = db.init(db.connect())
    report = indexer.Report(verbose=not args.quiet)

    last = [0.0]

    def progress(done, total, label):
        if args.quiet:
            return
        now = time.time()
        if now - last[0] < 0.2 and done < total:
            return
        last[0] = now
        print("\r  %-58s %d/%d" % (label[:58], done, total), end="", flush=True)
        if done >= total:
            print()

    full_scan(con, report=report, progress=progress, with_git=not args.no_git, measure=args.measure)
    if args.report:
        _print_report(con, report)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
