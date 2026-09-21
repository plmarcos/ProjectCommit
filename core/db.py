"""Indice local em SQLite: schema, migrations e helpers de acesso.

Este e' o UNICO banco em que o ProjectCommit escreve. As bases das IAs
(.codex/state_*.sqlite, .gemini/antigravity/conversations/*.db) sao sempre
abertas via open_readonly(), que usa o modo 'ro' da URI do SQLite.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time

from . import settings

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Controle de leitura incremental. JSONL e' append-only: guardamos o offset em
-- bytes ja' consumido e na proxima varredura damos seek() nele.
CREATE TABLE IF NOT EXISTS source_files (
    path         TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL    NOT NULL DEFAULT 0,
    last_offset  INTEGER NOT NULL DEFAULT 0,
    last_indexed REAL    NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY,
    key           TEXT UNIQUE NOT NULL,   -- caminho normalizado em caixa baixa
    path          TEXT NOT NULL,          -- caminho para exibir
    name          TEXT NOT NULL,
    root          TEXT,                   -- raiz configurada; NULL = descoberto via IA
    on_disk       INTEGER NOT NULL DEFAULT 1,
    discovered    INTEGER NOT NULL DEFAULT 0,
    size_bytes    INTEGER,
    file_count    INTEGER,
    first_seen    REAL,
    last_activity REAL,
    fs_mtime      REAL
);
CREATE INDEX IF NOT EXISTS idx_projects_activity ON projects(last_activity DESC);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,        -- "<ai>:<native_id>"
    ai           TEXT NOT NULL,           -- claude | codex | antigravity
    native_id    TEXT NOT NULL,
    project_id   INTEGER REFERENCES projects(id),
    cwd          TEXT,
    title        TEXT,
    model        TEXT,
    cli_version  TEXT,
    git_branch   TEXT,
    started_at   REAL,
    ended_at     REAL,
    msg_count    INTEGER NOT NULL DEFAULT 0,
    tok_in       INTEGER NOT NULL DEFAULT 0,
    tok_out      INTEGER NOT NULL DEFAULT 0,
    tok_cache_r  INTEGER NOT NULL DEFAULT 0,
    tok_cache_w  INTEGER NOT NULL DEFAULT 0,
    tok_total    INTEGER NOT NULL DEFAULT 0,
    sub_count    INTEGER NOT NULL DEFAULT 0,
    first_prompt TEXT,
    source_path  TEXT,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    extra        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_ended   ON sessions(ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_ai      ON sessions(ai);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    project_id INTEGER REFERENCES projects(id),
    ai         TEXT NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,             -- prompt | session | commit | artifact
    text       TEXT,
    uid        TEXT UNIQUE                -- dedup entre reindexacoes
);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, ts DESC);

CREATE TABLE IF NOT EXISTS git_state (
    project_id   INTEGER PRIMARY KEY REFERENCES projects(id),
    has_git      INTEGER NOT NULL DEFAULT 0,
    branch       TEXT,
    commit_count INTEGER,
    dirty_count  INTEGER,
    ahead        INTEGER,
    behind       INTEGER,
    remote       TEXT,
    last_ts      REAL,
    last_msg     TEXT,
    last_author  TEXT,
    error        TEXT,
    checked_at   REAL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    session_id TEXT,
    ai         TEXT,
    kind       TEXT NOT NULL,             -- image | context
    path       TEXT UNIQUE NOT NULL,
    name       TEXT,
    size       INTEGER,
    mtime      REAL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id, kind);

-- Consumo agregado por DIA. Existe porque o total por sessao nao responde
-- "meu gasto esta' subindo?": uma sessao de 6 dias vira um ponto so'.
-- Preenchido pelo coletor do Claude, que grava usage por mensagem com hora.
-- O Codex NAO entra aqui: ele so' expoe o total acumulado da thread.
-- session_id entra na chave para que uma sessao relida do ZERO possa ter a
-- propria contribuicao apagada antes de ser recontada. Sem ele o balde e' de
-- todo mundo e nao havia como desfazer -- foi assim que o grafico diario ficou
-- com o dobro do consumo real. Ver scanner.recontar_claude().
CREATE TABLE IF NOT EXISTS daily_usage (
    day         TEXT    NOT NULL,          -- YYYY-MM-DD, hora local
    ai          TEXT    NOT NULL,
    model       TEXT    NOT NULL DEFAULT '',
    project_id  INTEGER,
    session_id  TEXT    NOT NULL DEFAULT '',
    tok_in      INTEGER NOT NULL DEFAULT 0,
    tok_out     INTEGER NOT NULL DEFAULT 0,
    tok_cache_r INTEGER NOT NULL DEFAULT 0,
    tok_cache_w INTEGER NOT NULL DEFAULT 0,
    msgs        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, ai, model, project_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_day     ON daily_usage(day);
CREATE INDEX IF NOT EXISTS idx_daily_sessao  ON daily_usage(session_id);

-- Imagens embutidas na conversa (o que voce COLA nao vira arquivo em disco).
-- Guardamos so' a referencia: arquivo + deslocamento da linha + indice do bloco.
-- Os bytes ficam onde estao e sao decodificados na hora de servir.
CREATE TABLE IF NOT EXISTS inline_media (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    project_id  INTEGER,
    ai          TEXT    NOT NULL,
    origem      TEXT    NOT NULL,      -- entrada (voce colou) | saida (a IA gerou)
    offset      INTEGER NOT NULL,
    length      INTEGER NOT NULL,
    bloco       INTEGER NOT NULL,
    media_type  TEXT,
    bytes       INTEGER,
    ts          REAL,
    source_path TEXT    NOT NULL,
    UNIQUE(source_path, offset, bloco)
);
CREATE INDEX IF NOT EXISTS idx_media_projeto ON inline_media(project_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_media_sessao  ON inline_media(session_id);

-- Arquivos que a IA EDITOU, tirados dos blocos de ferramenta da propria
-- conversa. Guardamos o caminho absoluto; o recorte "dentro do projeto" e' da
-- consulta, porque a IA tambem edita fora dele e isso nao deve poluir a lista.
CREATE TABLE IF NOT EXISTS touched_files (
    session_id TEXT    NOT NULL,
    project_id INTEGER,
    ai         TEXT    NOT NULL,
    path       TEXT    NOT NULL,
    tool       TEXT,
    edits      INTEGER NOT NULL DEFAULT 0,
    first_ts   REAL,
    last_ts    REAL,
    PRIMARY KEY (session_id, path)
);
CREATE INDEX IF NOT EXISTS idx_touched_projeto ON touched_files(project_id, last_ts DESC);
CREATE INDEX IF NOT EXISTS idx_touched_path    ON touched_files(path);

-- Marcadores (TODO/FIXME/BUG/HACK) nos arquivos que a IA edita. NAO e' a arvore
-- inteira do projeto: medido, ela da' 3.984 marcadores em 116 s, quase todos de
-- dependencia de terceiro. Ver core/sinais.py.
-- A linha com nivel='limpo' registra "li este mtime e nao achei nada", para o
-- arquivo sem marcador nao ser relido a cada varredura.
CREATE TABLE IF NOT EXISTS sinais (
    path   TEXT    NOT NULL,
    mtime  REAL    NOT NULL,
    nivel  TEXT    NOT NULL,          -- alto | medio | baixo | limpo
    marca  TEXT    NOT NULL,          -- BUG, FIXME, TODO, HACK, XXX, WIP
    linha  INTEGER NOT NULL,
    texto  TEXT,
    PRIMARY KEY (path, linha)
);
CREATE INDEX IF NOT EXISTS idx_sinais_path ON sinais(path);

-- Indice de mensagens por sessao: onde cada mensagem comeca dentro do arquivo.
-- E' o que permite paginar uma conversa dentro de um rollout de 491 MB sem
-- reler nada -- ver core/transcript.py.
CREATE TABLE IF NOT EXISTS msg_index (
    session_id TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    offset     INTEGER NOT NULL,
    length     INTEGER NOT NULL,
    role       TEXT,
    ts         REAL,
    preview    TEXT,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS msg_state (
    session_id     TEXT PRIMARY KEY,
    indexed_size   INTEGER NOT NULL DEFAULT 0,
    indexed_offset INTEGER NOT NULL DEFAULT 0,
    count          INTEGER NOT NULL DEFAULT 0,
    built_at       REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    body,
    title,
    ref UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


def connect(path=None):
    con = sqlite3.connect(path or settings.index_path(), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init(con):
    _migrar(con)
    con.executescript(SCHEMA)
    cur = con.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        con.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),)
        )
    con.commit()
    return con


def _migrar(con):
    """Ajustes em tabela ja' existente, ANTES de rodar o schema.

    A ordem importa: o schema cria um indice sobre `daily_usage(session_id)`, e
    num indice antigo essa coluna nao existe -- rodar o script primeiro morre com
    "no such column: session_id" e nem chega aqui.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(daily_usage)")}
    if cols and "session_id" not in cols:
        # A chave primaria mudou, entao ALTER nao resolve. Descartar e' seguro:
        # o unico que escreve aqui e' o coletor do Claude, e a recontagem que
        # acompanha esta versao refaz a tabela inteira.
        con.execute("DROP TABLE daily_usage")
        con.commit()


def open_readonly(path, retries=3):
    """Abre um sqlite de terceiro sem escrever nele.

    Detalhe descoberto medindo a pasta antes e depois de uma varredura: `mode=ro`
    sozinho NAO e' suficiente. Em bancos WAL o SQLite precisa mapear o indice
    compartilhado, e isso cria/atualiza o arquivo `<db>-shm` -- o mtime das 7
    conversas do Antigravity mudava a cada leitura.

    Regra usada aqui:
      * existe `<db>-wal` com conteudo  -> o dono esta' com escritas pendentes;
        e' obrigatorio ler pelo WAL (mode=ro), senao os dados vem desatualizados.
        E' o caso do Codex enquanto o codex.exe roda.
      * nao existe WAL (ou esta' vazio) -> o banco esta' fechado e completo;
        `immutable=1` le direto do arquivo, sem shm e sem tocar em nada.
    """
    wal = str(path) + "-wal"
    try:
        immutable = os.path.getsize(wal) == 0
    except OSError:
        immutable = True

    uri = "file:" + str(path).replace("\\", "/").replace("?", "%3f") + "?mode=ro"
    if immutable:
        uri += "&immutable=1"

    last = None
    for attempt in range(retries):
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.Error as exc:
            last = exc
            time.sleep(0.25 * (attempt + 1))
    raise last


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def source_state(con, path):
    return con.execute("SELECT * FROM source_files WHERE path=?", (path,)).fetchone()


def set_source_state(con, path, kind, size, mtime, last_offset, note=None):
    con.execute(
        "INSERT INTO source_files(path,kind,size,mtime,last_offset,last_indexed,note) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
        "last_offset=excluded.last_offset, last_indexed=excluded.last_indexed, note=excluded.note",
        (path, kind, size, mtime, last_offset, time.time(), note),
    )


def fts_rowid(ref):
    """rowid deterministico a partir do ref.

    Existe por causa de uma regressao medida: `ref` e' UNINDEXED no FTS5, entao
    `DELETE ... WHERE ref=?` varre a tabela inteira. Com 5 mil linhas ninguem
    notava; ao indexar tambem as respostas da IA a tabela passou de 5,3 mil para
    17,3 mil e a etapa de busca saltou para 81 s numa varredura completa --
    5.400 reescritas x 17 mil linhas visitadas cada.

    Derivando o rowid do proprio ref, apagar vira busca por chave primaria.
    Colisao em 63 bits com ~20 mil linhas fica na casa de 1e-11.
    """
    return int.from_bytes(hashlib.blake2b(ref.encode("utf-8"), digest_size=8).digest(),
                          "big") >> 1


def fts_replace(con, ref, body, title=None):
    """Reescreve uma linha da busca, endereçada pelo rowid derivado do ref."""
    rid = fts_rowid(ref)
    con.execute("DELETE FROM search_fts WHERE rowid=?", (rid,))
    if body or title:
        con.execute(
            "INSERT INTO search_fts(rowid,body,title,ref) VALUES(?,?,?,?)",
            (rid, body or "", title or "", ref),
        )
