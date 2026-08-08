"""SQLite 저장소.

경주 단위 데이터는 재수집이 잦으므로 모든 쓰기를 upsert 로 처리한다. 원본 응답은
``raw_json`` 에 그대로 남겨서, 나중에 별칭 매핑이 틀렸다는 게 드러나도 API 를 다시
때리지 않고 로컬에서 재정규화할 수 있게 한다.
"""

from __future__ import annotations

import datetime as dt

from ..clock import today_kst
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

DEFAULT_DB = Path("data/horseai.sqlite")

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 경주 단위 메타 (출전표/성적 어느 쪽에서든 채워질 수 있다)
CREATE TABLE IF NOT EXISTS races (
    race_key     TEXT PRIMARY KEY,
    meet         TEXT NOT NULL,
    rc_date      TEXT NOT NULL,
    rc_no        INTEGER NOT NULL,
    rc_day       TEXT,
    rc_name      TEXT,
    distance     INTEGER,
    grade        TEXT,
    age_cond     TEXT,
    budam_type   TEXT,
    post_time    TEXT,
    field_size   INTEGER,
    prize1       REAL,
    weather      TEXT,
    track_cond   TEXT,
    has_result   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(rc_date);
CREATE INDEX IF NOT EXISTS idx_races_meet_date ON races(meet, rc_date);

-- 출전표: 경주 전에 확정되는 출주마 정보
CREATE TABLE IF NOT EXISTS entries (
    race_key      TEXT NOT NULL,
    chul_no       INTEGER NOT NULL,
    hr_no         TEXT NOT NULL,
    hr_name       TEXT,
    origin        TEXT,
    sex           TEXT,
    age           INTEGER,
    burden        REAL,
    rating        REAL,
    jk_no         TEXT,
    jk_name       TEXT,
    tr_no         TEXT,
    tr_name       TEXT,
    ow_no         TEXT,
    ow_name       TEXT,
    ilsu          INTEGER,
    career_prize  REAL,
    prize_1y      REAL,
    prize_6m      REAL,
    career_1st    INTEGER,
    career_2nd    INTEGER,
    career_3rd    INTEGER,
    career_starts INTEGER,
    y1_1st        INTEGER,
    y1_2nd        INTEGER,
    y1_3rd        INTEGER,
    y1_starts     INTEGER,
    raw_json      TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (race_key, chul_no),
    FOREIGN KEY (race_key) REFERENCES races(race_key)
);
CREATE INDEX IF NOT EXISTS idx_entries_hr ON entries(hr_no);
CREATE INDEX IF NOT EXISTS idx_entries_jk ON entries(jk_no);

-- 성적: 경주 후 착순/기록/배당률
CREATE TABLE IF NOT EXISTS results (
    race_key      TEXT NOT NULL,
    chul_no       INTEGER,
    hr_no         TEXT NOT NULL,
    hr_name       TEXT,
    ord           INTEGER,
    ord_note      TEXT,
    jk_no         TEXT,
    jk_name       TEXT,
    tr_no         TEXT,
    tr_name       TEXT,
    age           INTEGER,
    sex           TEXT,
    origin        TEXT,
    burden        REAL,
    rating        REAL,
    horse_weight  REAL,
    weight_delta  REAL,
    rank_rise     INTEGER,
    record_sec    REAL,
    margin        TEXT,
    win_odds      REAL,
    place_odds    REAL,
    jk_reduction  REAL,
    gear          TEXT,
    -- 구간 통과순위/기록. 각질(선행·선입·추입) 판정의 원천이다.
    s1f_rank      INTEGER,
    g1f_rank      INTEGER,
    c1_rank       INTEGER,
    c2_rank       INTEGER,
    c3_rank       INTEGER,
    c4_rank       INTEGER,
    s1f_sec       REAL,
    g3f_sec       REAL,
    g1f_sec       REAL,
    raw_json      TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (race_key, hr_no)
);
CREATE INDEX IF NOT EXISTS idx_results_hr ON results(hr_no);
CREATE INDEX IF NOT EXISTS idx_results_ord ON results(ord);

-- 예측: 경주 전에 생성해 고정(公開 후 수정 금지 — 적중률 신뢰의 근거)
CREATE TABLE IF NOT EXISTS predictions (
    race_key      TEXT NOT NULL,
    hr_no         TEXT NOT NULL,
    chul_no       INTEGER,
    p_win         REAL NOT NULL,
    p_place       REAL,
    pred_rank     INTEGER,
    model_version TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (race_key, hr_no, model_version)
);
CREATE INDEX IF NOT EXISTS idx_pred_race ON predictions(race_key);

-- 경주 시뮬레이션: 미리보기 대본과 신뢰도.
-- 예측과 함께 동결한다 — 나중에 다시 돌리면 그때의 이력이 반영돼
-- '공개 당시 보여준 전개'와 달라진다.
CREATE TABLE IF NOT EXISTS simulations (
    race_key    TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    conf_score  INTEGER,
    conf_label  TEXT,
    conf_desc   TEXT,
    n_sims      INTEGER,
    noise_scale REAL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 예상 코멘트 (LLM 생성) — 경주 단위 1건
CREATE TABLE IF NOT EXISTS commentaries (
    race_key   TEXT PRIMARY KEY,
    headline   TEXT,
    body       TEXT,
    model      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 조교(훈련) 현황. KRA 는 '오늘 것'만 제공하고 과거 조회를 지원하지 않는다.
-- 따라서 매일 받아 우리가 직접 이력을 쌓아야 언젠가 피처가 된다.
-- 하루 늦으면 그 하루는 영구히 사라진다.
CREATE TABLE IF NOT EXISTS daily_training (
    meet       TEXT NOT NULL,
    trng_dt    TEXT NOT NULL,
    hr_name    TEXT NOT NULL,
    belo_no    TEXT,
    trng_cnt   INTEGER,
    rider      TEXT,
    remark     TEXT,
    raw_json   TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meet, trng_dt, hr_name)
);
CREATE INDEX IF NOT EXISTS idx_train_dt ON daily_training(trng_dt);

-- 경주 전 계측 마체중. 개최일 당일에만 값이 실린다.
CREATE TABLE IF NOT EXISTS entry_weight (
    meet       TEXT NOT NULL,
    rc_date    TEXT NOT NULL,
    hr_name    TEXT NOT NULL,
    rc_no      INTEGER,
    chul_no    INTEGER,
    weight     REAL,
    weight_delta REAL,
    raw_json   TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meet, rc_date, hr_name)
);

-- 출주 취소. 예상에는 반영하지 않고(당일 급변), 결과·적중률 집계에서 제외한다.
CREATE TABLE IF NOT EXISTS cancellations (
    race_key   TEXT NOT NULL,
    hr_no      TEXT NOT NULL,
    hr_name    TEXT,
    chul_no    INTEGER,
    reason     TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (race_key, hr_no)
);

-- 수집 이력 (증분 수집용)
CREATE TABLE IF NOT EXISTS fetch_log (
    endpoint   TEXT NOT NULL,
    meet       TEXT NOT NULL,
    rc_date    TEXT NOT NULL,
    n_records  INTEGER NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (endpoint, meet, rc_date)
);
"""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 스키마가 뒤늦게 늘어나도 기존 DB를 버리지 않도록 하는 가벼운 마이그레이션.
# (테이블, 컬럼, 타입) — 이미 있으면 조용히 건너뛴다.
MIGRATIONS = [
    ("results", col, "INTEGER")
    for col in ("s1f_rank", "g1f_rank", "c1_rank", "c2_rank", "c3_rank", "c4_rank")
] + [("results", col, "REAL") for col in ("s1f_sec", "g3f_sec", "g1f_sec")] + [
    # 각질과 특징 태그는 예측 시점의 판단이므로 예측과 함께 동결한다.
    # 나중에 재계산하면 그때의 이력이 반영돼 '공개 당시 화면'과 달라진다.
    # 연령·성별은 예측에 중요한데 초기 스키마에서 빠져 있었다.
    ("results", "age", "INTEGER"),
    ("results", "sex", "TEXT"),
    ("results", "origin", "TEXT"),
    ("results", "weight_delta", "REAL"),
    ("results", "rank_rise", "INTEGER"),
    ("predictions", "style_code", "TEXT"),
    ("predictions", "tags", "TEXT"),
    # 예상 기호가 '2착 이내 수준'을 뜻하므로 그 확률을 예측과 함께 동결한다.
    # 나중에 다시 계산하면 공개 당시 화면과 기호가 달라질 수 있다.
    ("predictions", "p_top2", "REAL"),
    # 일별훈련 상세(API18_1) — 마필 단위. 기존 서울 전용 조교표(API329)보다
    # 넓고 깊어서 이쪽을 주 자료로 삼는다.
    ("daily_training", "hr_no", "TEXT"),
    ("daily_training", "tr_name", "TEXT"),
    ("daily_training", "part", "TEXT"),
    ("daily_training", "pr_gubun", "TEXT"),      # 기승자 구분 (기수/조교사/조교보…)
    ("daily_training", "tr_term", "INTEGER"),    # 훈련 시간(초)
    ("daily_training", "run1_cnt", "INTEGER"),   # 구보
    ("daily_training", "run2_cnt", "INTEGER"),   # 습보
    ("daily_training", "chul_gubun", "TEXT"),    # 금주/차주 출전예정
    ("daily_training", "st_time", "TEXT"),
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, col, coltype in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.commit()


@contextmanager
def session(path: Path | str = DEFAULT_DB) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def upsert(conn: sqlite3.Connection, table: str, rows: Iterable[Dict[str, Any]],
           key_cols: List[str]) -> int:
    """존재하는 컬럼만 골라 upsert. 스키마에 없는 키는 조용히 버린다.

    **빈 값으로는 덮어쓰지 않는다.** 같은 행을 여러 API 가 조각조각 채운다 —
    발주시각·출주두수는 출전표에만 있고 착순·배당은 성적에만 있다. 들어온 값이
    비었다고 기존 값을 지우면, 성적을 수집하는 순간 발주시각이 사라진다
    (실제로 그렇게 되어 발주 시각 기준 예측 동결이 무력화됐다).
    """
    rows = [r for r in rows if r]
    if not rows:
        return 0
    cols = [c for c in _table_columns(conn, table) if c != "updated_at"]
    usable = [c for c in cols if any(c in r for r in rows)]
    if not usable:
        return 0

    placeholders = ",".join("?" for _ in usable)
    update_cols = [c for c in usable if c not in key_cols]
    set_clause = ", ".join(f"{c}=COALESCE(excluded.{c}, {table}.{c})" for c in update_cols)
    has_updated = "updated_at" in _table_columns(conn, table)
    if has_updated:
        set_clause = (set_clause + ", " if set_clause else "") + "updated_at=datetime('now')"

    sql = (
        f"INSERT INTO {table} ({','.join(usable)}) VALUES ({placeholders}) "
        f"ON CONFLICT({','.join(key_cols)}) DO UPDATE SET {set_clause}"
    )
    payload = [tuple(r.get(c) for c in usable) for r in rows]
    conn.executemany(sql, payload)
    return len(payload)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def log_fetch(conn: sqlite3.Connection, endpoint: str, meet: str, rc_date: str, n: int) -> None:
    conn.execute(
        "INSERT INTO fetch_log(endpoint,meet,rc_date,n_records) VALUES(?,?,?,?) "
        "ON CONFLICT(endpoint,meet,rc_date) DO UPDATE SET "
        "n_records=excluded.n_records, fetched_at=datetime('now')",
        (endpoint, str(meet), rc_date, n),
    )


def already_fetched(conn: sqlite3.Connection, endpoint: str, meet: str, rc_date: str) -> bool:
    # 0건이었던 날도 '확인 완료'로 본다. 경마가 없던 날을 매번 다시 묻는 것은
    # 하루 800회가 넘는 낭비이고, 일일 호출 한도를 그것만으로 태워 버린다.
    row = conn.execute(
        "SELECT n_records FROM fetch_log WHERE endpoint=? AND meet=? AND rc_date=?",
        (endpoint, str(meet), rc_date),
    ).fetchone()
    return row is not None


def prune_raw_json(conn: sqlite3.Connection, keep_days: int = 180) -> int:
    """오래된 행의 ``raw_json`` 을 비운다.

    raw_json 은 별칭 매핑이 틀렸을 때 API 재호출 없이 로컬에서 재정규화하기 위한
    보험이다. 그 가치는 수집 초기에 집중되는 반면 용량은 계속 늘어난다(5년치면
    100MB를 훌쩍 넘긴다). 최근 구간만 남기고 비워 DB를 운반 가능한 크기로 유지한다.
    """
    cutoff = (today_kst() - dt.timedelta(days=keep_days)).isoformat()
    total = 0
    for table in ("entries", "results"):
        cur = conn.execute(
            f"UPDATE {table} SET raw_json = NULL WHERE raw_json IS NOT NULL AND race_key IN "
            f"(SELECT race_key FROM races WHERE rc_date < ?)",
            (cutoff,),
        )
        total += cur.rowcount or 0
    conn.commit()
    conn.execute("VACUUM")
    return total


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    out = {}
    for t in ("races", "entries", "results", "predictions", "commentaries"):
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        except sqlite3.Error:
            out[t] = 0
    return out
