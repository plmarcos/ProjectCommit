"""Escritas no indice: upsert aditivo de sessoes, eventos e artefatos.

Todos os coletores passam por aqui, para que a indexacao incremental funcione:
uma sessao pode ser lida em varias passadas (o JSONL cresce), entao os campos
numericos SOMAM e os descritivos ficam com o valor mais recente nao-nulo.
"""
from __future__ import annotations

import os

from . import util

_NUMERIC = ("msg_count", "sub_count", "tok_in", "tok_out", "tok_cache_r", "tok_cache_w", "tok_total")
_LATEST = (
    "project_id", "cwd", "title", "model", "cli_version", "git_branch",
    "first_prompt", "source_path", "extra",
)


class Report:
    """Acumulador de estatisticas e avisos de uma varredura."""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.counts = {}
        self.warnings = []
        self.timings = {}
        #: Sessoes cujo arquivo foi lido do byte ZERO nesta varredura. O coletor
        #: ja' extraiu tudo delas, entao a passada de backfill pode pular --
        #: sem isto um indice novo leria os 5,1 GB duas vezes.
        self.completas = set()

    def bump(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    def warn(self, msg):
        self.warnings.append(msg)
        if self.verbose:
            print("  [aviso]", msg)

    def log(self, msg):
        if self.verbose:
            print("  " + msg)


#: Pastas do perfil que nunca sao projeto, mesmo que uma IA tenha rodado dentro.
_PESSOAIS = ("downloads", "desktop", "documents", "documentos", "imagens",
             "pictures", "music", "musicas", "videos", "onedrive")

#: Tripas de ferramenta e area de rascunho -- nunca projeto de ninguem.
#: `Documents\Codex\<data>\<slug do prompt>` e' o sandbox que o proprio Codex
#: cria por conversa: as pastas se chamam "chat", "esse-grafico-aqui-oque",
#: "c-users-eu-downloads-musicas-sem". Nao sao projetos, sao recados.
_RUIDO = ("\\appdata\\", "\\.codex\\", "\\.claude\\", "\\.gemini\\", "\\.vscode\\",
          "\\node_modules\\", "\\documents\\codex\\", "\\onedrive\\",
          "\\program files", "\\windows\\", "\\$recycle.bin\\", "\\temp\\")

#: Sinais de que um diretorio e' mesmo um projeto. Usado so' para adotar coisa
#: de dentro do perfil do usuario, onde o padrao e' NAO adotar.
MARCAS_DE_PROJETO = (
    ".git", "CLAUDE.md", "AGENTS.md", ".claude", "package.json",
    "pyproject.toml", "requirements.txt", "app.json", "index.html", "README.md",
    "LEIA-ME.md", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
)


def nunca_projeto(roots=None):
    """Caminhos que NAO podem virar projeto, por mais que uma IA trabalhe neles.

    Vale para as raizes configuradas (a raiz nao e' projeto, os filhos dela sao),
    para a pasta do usuario e para as pastas pessoais dentro dela.
    """
    from . import settings

    if roots is None:
        roots = settings.load().get("roots") or []
    proibidas = {util.path_key(r) for r in roots if r}
    casa = os.path.expanduser("~")
    proibidas.add(util.path_key(casa))
    for nome in _PESSOAIS:
        proibidas.add(util.path_key(os.path.join(casa, nome)))
    proibidas.discard(None)
    return proibidas


def pode_adotar(caminho, proibidas, roots):
    """Este `cwd` pode virar um projeto novo?

    Aprendido em duas rodadas. Primeiro `C:\\Users\\<voce>` foi adotado porque uma
    sessao rodou ali, e pela regra do prefixo mais longo passou a recolher TUDO
    que nao casasse com um projeto mais fundo -- 447 arquivos -- virando o
    primeiro alerta do Radar. Ao proibir so' aquele caminho, os `cwd` de dentro
    dele viraram DEZ projetos novos, todos area de rascunho de ferramenta.

    A regra que sobrou: dentro de uma raiz configurada, adota. Fora dela e dentro
    do perfil do usuario, so' adota o que PARECE projeto -- tem `.git`, um
    manifesto, um CLAUDE.md. Projeto inventado e' pior que nenhum: sem projeto a
    sessao aparece como "sem projeto", que e' a verdade.
    """
    chave = util.path_key(caminho)
    if not chave or chave in proibidas:
        return False
    if any(r in chave + util.SEP for r in _RUIDO):
        return False
    if any(util.is_under(caminho, r) for r in roots if r):
        return True
    if util.is_under(caminho, os.path.expanduser("~")):
        return any(
            os.path.exists(os.path.join(caminho, m)) for m in MARCAS_DE_PROJETO
        )
    return True


class ProjectResolver:
    """Mapeia um cwd de sessao para uma linha de projects.

    Prefere o casamento mais longo: um cwd em subpasta pertence ao projeto pai.
    """

    def __init__(self, con, adopt_discovered=True, roots=None):
        from . import settings

        self.con = con
        self.adopt = adopt_discovered
        self.roots = roots if roots is not None else (settings.load().get("roots") or [])
        self.proibidas = nunca_projeto(self.roots)
        self.reload()

    def reload(self):
        self.by_key = {}
        for row in self.con.execute("SELECT id, key, path FROM projects"):
            self.by_key[row["key"]] = row["id"]
        # Casamento por prefixo precisa do mais longo primeiro.
        self._sorted = sorted(self.by_key.items(), key=lambda kv: len(kv[0]), reverse=True)

    def resolve(self, path, create=True):
        key = util.path_key(path)
        if not key:
            return None
        hit = self.by_key.get(key)
        if hit:
            return hit
        for pkey, pid in self._sorted:
            if key.startswith(pkey + util.SEP):
                return pid
        if not (create and self.adopt):
            return None
        if not pode_adotar(path, self.proibidas, self.roots):
            return None
        return self.add_discovered(path)

    def add_discovered(self, path):
        clean = util.clean_path(path)
        key = util.path_key(clean)
        import os

        cur = self.con.execute(
            "INSERT INTO projects(key, path, name, root, on_disk, discovered, first_seen) "
            "VALUES(?,?,?,NULL,?,1,?) ON CONFLICT(key) DO NOTHING",
            (key, clean, util.basename(clean) or clean, 1 if os.path.isdir(clean) else 0, util.now()),
        )
        if cur.lastrowid:
            self.by_key[key] = cur.lastrowid
            self._sorted = sorted(self.by_key.items(), key=lambda kv: len(kv[0]), reverse=True)
            return cur.lastrowid
        self.reload()
        return self.by_key.get(key)


def merge_session(con, sid, ai, native_id, absolute=False, **fields):
    """Upsert aditivo. Numeros somam; descritivos sobrescrevem se nao forem nulos.

    absolute=True quando a fonte ja' entrega o total acumulado em vez de um delta
    (o Codex guarda tokens_used somado na propria tabela threads).
    """
    row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        cols = ["id", "ai", "native_id"]
        vals = [sid, ai, native_id]
        for name in _NUMERIC:
            cols.append(name)
            vals.append(int(fields.get(name) or 0))
        for name in _LATEST + ("started_at", "ended_at"):
            cols.append(name)
            vals.append(fields.get(name))
        cols.append("size_bytes")
        vals.append(int(fields.get("size_bytes") or 0))  # coluna NOT NULL
        con.execute(
            "INSERT INTO sessions(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
            vals,
        )
        return

    sets, vals = [], []
    for name in _NUMERIC:
        if name not in fields or fields.get(name) is None:
            continue
        value = int(fields.get(name) or 0)
        if absolute:
            sets.append("%s = ?" % name)
            vals.append(value)
        elif value:
            sets.append("%s = %s + ?" % (name, name))
            vals.append(value)
    for name in _LATEST:
        if fields.get(name) is not None:
            sets.append("%s = ?" % name)
            vals.append(fields[name])
    if fields.get("started_at") is not None:
        sets.append("started_at = MIN(COALESCE(started_at, ?), ?)")
        vals.extend([fields["started_at"], fields["started_at"]])
    if fields.get("ended_at") is not None:
        sets.append("ended_at = MAX(COALESCE(ended_at, ?), ?)")
        vals.extend([fields["ended_at"], fields["ended_at"]])
    if fields.get("size_bytes") is not None:
        sets.append("size_bytes = ?")
        vals.append(fields["size_bytes"])
    if not sets:
        return
    vals.append(sid)
    con.execute("UPDATE sessions SET %s WHERE id=?" % ", ".join(sets), vals)


def add_daily_usage(con, buckets, session_id=""):
    """Soma consumo no balde (dia, ia, modelo, projeto, sessao).

    SOMA em vez de sobrescrever porque a leitura e' incremental: a mesma sessao
    pode ser lida em varias passadas conforme o arquivo cresce. A sessao entra na
    chave para que uma releitura do zero possa apagar a propria contribuicao
    antes de recontar -- ver limpar_daily_usage().
    """
    if not buckets:
        return
    con.executemany(
        "INSERT INTO daily_usage(day, ai, model, project_id, session_id, tok_in, tok_out, "
        "  tok_cache_r, tok_cache_w, msgs) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day, ai, model, project_id, session_id) DO UPDATE SET "
        "  tok_in = tok_in + excluded.tok_in, tok_out = tok_out + excluded.tok_out, "
        "  tok_cache_r = tok_cache_r + excluded.tok_cache_r, "
        "  tok_cache_w = tok_cache_w + excluded.tok_cache_w, msgs = msgs + excluded.msgs",
        [
            (dia, ai, modelo or "", pid, session_id, v["in"], v["out"], v["cr"], v["cw"], v["n"])
            for (dia, ai, modelo, pid), v in buckets.items()
        ],
    )


#: Colunas que SOMAM em merge_session. Zerar todas e' o que torna uma releitura
#: do zero segura -- sem isso o arquivo relido soma por cima do que ja' estava.
def zerar_sessao(con, sid):
    """Descarta o consumo acumulado de uma sessao, para ela ser recontada."""
    con.execute(
        "UPDATE sessions SET msg_count=0, sub_count=0, tok_in=0, tok_out=0, "
        "tok_cache_r=0, tok_cache_w=0, tok_total=0 WHERE id=?",
        (sid,),
    )
    con.execute("DELETE FROM daily_usage WHERE session_id=?", (sid,))


def add_event(con, uid, ai, ts, kind, text=None, session_id=None, project_id=None):
    """INSERT OR IGNORE: uid deduplica quando um arquivo e' reindexado do zero."""
    if ts is None:
        return False
    cur = con.execute(
        "INSERT OR IGNORE INTO events(session_id, project_id, ai, ts, kind, text, uid) "
        "VALUES(?,?,?,?,?,?,?)",
        (session_id, project_id, ai, ts, kind, util.clip(text, 2000), uid),
    )
    return bool(cur.rowcount)


def add_artifact(con, path, kind, ai=None, project_id=None, session_id=None, name=None):
    st = util.stat_or_none(path)
    if st is None:
        return False
    cur = con.execute(
        "INSERT INTO artifacts(project_id, session_id, ai, kind, path, name, size, mtime) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "size=excluded.size, mtime=excluded.mtime, project_id=COALESCE(excluded.project_id, project_id)",
        (project_id, session_id, ai, kind, path, name or util.basename(path), st.st_size, st.st_mtime),
    )
    return bool(cur.rowcount)


def refresh_project_activity(con):
    """Recalcula last_activity de cada projeto a partir das sessoes indexadas.

    Sem `WHERE EXISTS`: o projeto que PERDEU suas sessoes tambem precisa perder a
    data. Uma sessao troca de dono quando um projeto mais fundo e' descoberto
    (`teste` -> `teste\\Work`), e o antigo ficava com a data velha para sempre --
    cinco projetos apareciam no Radar com data de atividade de IA e zero sessoes.
    Com a coluna nula o card cai no mtime da pasta, que e' a verdade.
    """
    con.execute(
        "UPDATE projects SET last_activity = ("
        "  SELECT MAX(COALESCE(s.ended_at, s.started_at)) FROM sessions s"
        "  WHERE s.project_id = projects.id"
        ")"
    )
