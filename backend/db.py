import json
import os
import time
from datetime import datetime, timezone

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_batches (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    status TEXT NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS landed_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    UNIQUE (station_code, observed_at)
);

CREATE TABLE IF NOT EXISTS reject_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_attempts (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    reject_row_id BIGINT,
    original_line_no INTEGER,
    new_line_no INTEGER,
    outcome TEXT NOT NULL,
    disposition TEXT,
    disposition_label TEXT,
    mismatch BOOLEAN NOT NULL DEFAULT FALSE,
    mismatch_note TEXT,
    original_reason TEXT,
    replay_reason TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    attempted_raw TEXT,
    attempted_by TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS replay_settings (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    required_disposition TEXT NOT NULL DEFAULT 'keep',
    allow_edit BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by TEXT,
    updated_at TIMESTAMPTZ
);
"""

# Columns added after the first release of the table.  CREATE TABLE IF NOT
# EXISTS does not extend an existing table, so these run on every boot.
MIGRATIONS = """
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS reasons JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS disposition TEXT;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS disposition_date DATE;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS disposition_by TEXT;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS replaced_by_id BIGINT;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'intake';
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS source_reject_id BIGINT;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'intake';
ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS source_reject_id BIGINT;

INSERT INTO replay_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

UPDATE reject_rows
SET reasons = jsonb_build_array(
    jsonb_build_object('reason', reason, 'source', 'intake', 'at', NULL, 'by', NULL)
)
WHERE reasons IS NULL OR reasons = '[]'::jsonb;
"""

DISPOSITIONS = {
    "keep": "原行留在未接纳",
    "replace": "原行撤下，换新未接纳行",
}

OPEN_STATUSES = ("active", "queued", "replaying")

# Shared/exclusive advisory lock: replays hold it shared, changing the replay
# settings needs it exclusive.  That is what keeps settings frozen while a
# replay is in flight, without depending on uncommitted status rows.
REPLAY_LOCK_KEY = 918_273_645
REPLAY_ROW_NAMESPACE = 91_827


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reason_entry(reason: str, source: str, user: str | None):
    return {"reason": reason, "source": source, "at": _now_iso(), "by": user}


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                conn.execute(MIGRATIONS)
                count = conn.execute("SELECT COUNT(*) AS n FROM intake_batches").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _append_reason(conn, row_id: int, entry: dict):
    conn.execute(
        """
        UPDATE reject_rows
        SET reasons = COALESCE(reasons, '[]'::jsonb) || %s::jsonb
        WHERE id = %s
        """,
        (json.dumps([entry], ensure_ascii=False), row_id),
    )


def _next_line_no(conn, batch_id: int) -> int:
    row = conn.execute(
        """
        SELECT GREATEST(
            COALESCE((SELECT MAX(line_no) FROM landed_rows WHERE batch_id = %s), 0),
            COALESCE((SELECT MAX(line_no) FROM reject_rows WHERE batch_id = %s), 0)
        ) + 1 AS n
        """,
        (batch_id, batch_id),
    ).fetchone()
    return row["n"]


def _recompute_batch(conn, batch_id: int):
    accepted = conn.execute(
        "SELECT COUNT(*) AS n FROM landed_rows WHERE batch_id = %s", (batch_id,)
    ).fetchone()["n"]
    rejected = conn.execute(
        "SELECT COUNT(*) AS n FROM reject_rows WHERE batch_id = %s AND status = ANY(%s)",
        (batch_id, list(OPEN_STATUSES)),
    ).fetchone()["n"]
    if accepted and not rejected:
        status = "已归集"
    elif accepted:
        status = "部分接纳"
    elif rejected:
        status = "未接纳"
    else:
        status = "已归集"
    conn.execute(
        """
        UPDATE intake_batches
        SET status = %s, accepted_count = %s, rejected_count = %s
        WHERE id = %s
        """,
        (status, accepted, rejected, batch_id),
    )


def _insert_batch(conn, title: str, raw_text: str, created_by: str):
    from parser import parse_telegram_line

    batch = conn.execute(
        """
        INSERT INTO intake_batches (title, raw_text, status, accepted_count, rejected_count, created_by)
        VALUES (%s, %s, '处理中', 0, 0, %s)
        RETURNING id
        """,
        (title, raw_text, created_by),
    ).fetchone()
    seen = set()
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parsed, reason = parse_telegram_line(raw_line)
        if reason:
            _insert_reject(conn, batch["id"], line_no, raw_line.strip(), reason)
            continue
        key = (parsed["station_code"], parsed["observed_at"])
        if key in seen:
            reason = "同一批里观测时刻重复"
        else:
            existing = conn.execute(
                "SELECT id FROM landed_rows WHERE station_code = %s AND observed_at = %s",
                key,
            ).fetchone()
            reason = "这个站这个时刻已经落过库" if existing else None
        if reason:
            _insert_reject(conn, batch["id"], line_no, raw_line.strip(), reason)
            continue
        seen.add(key)
        conn.execute(
            """
            INSERT INTO landed_rows (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                batch["id"],
                line_no,
                parsed["station_code"],
                parsed["observed_at"],
                parsed["tide_cm"],
                parsed["quality_flag"],
            ),
        )
    _recompute_batch(conn, batch["id"])
    return batch["id"]


def _insert_reject(conn, batch_id, line_no, raw_line, reason, origin="intake", source_reject_id=None):
    return conn.execute(
        """
        INSERT INTO reject_rows (batch_id, line_no, raw_line, reason, reasons, status, origin, source_reject_id)
        VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)
        RETURNING id
        """,
        (
            batch_id,
            line_no,
            raw_line,
            reason,
            Jsonb([_reason_entry(reason, origin, None)]),
            origin,
            source_reject_id,
        ),
    ).fetchone()["id"]


def _row_snapshot(row):
    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "line_no": row["line_no"],
        "raw_line": row["raw_line"],
        "reason": row["reason"],
        "status": row["status"],
        "failure_count": row["failure_count"],
        "disposition": row["disposition"],
        "disposition_label": DISPOSITIONS.get(row["disposition"]),
        "disposition_date": row["disposition_date"].isoformat() if row["disposition_date"] else None,
        "disposition_by": row["disposition_by"],
        "replaced_by_id": row["replaced_by_id"],
        "origin": row["origin"],
        "source_reject_id": row["source_reject_id"],
        "claimed_by": row["claimed_by"],
        "replayed": row["status"] == "replayed",
        "withdrawn": row["status"] == "withdrawn",
    }


def get_settings(conn):
    row = conn.execute(
        """
        SELECT id, required_disposition, allow_edit, updated_by, updated_at
        FROM replay_settings WHERE id = 1
        """
    ).fetchone()
    replaying = conn.execute(
        "SELECT COUNT(*) AS n FROM reject_rows WHERE status = 'replaying'"
    ).fetchone()["n"]
    return {
        "required_disposition": row["required_disposition"],
        "required_disposition_label": DISPOSITIONS.get(row["required_disposition"]),
        "allow_edit": row["allow_edit"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"].isoformat(sep=" ", timespec="minutes")
        if row["updated_at"]
        else None,
        "replay_in_progress": replaying > 0,
        "dispositions": [{"value": k, "label": v} for k, v in DISPOSITIONS.items()],
    }


def update_settings(conn, username: str, required_disposition: str, allow_edit: bool):
    if required_disposition not in DISPOSITIONS:
        return 400, "去向只能是 keep 或 replace"
    # Exclusive advisory lock conflicts with the shared lock every replay
    # holds, so an in-flight replay blocks the settings change outright.
    acquired = conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s) AS ok", (REPLAY_LOCK_KEY,)
    ).fetchone()["ok"]
    if not acquired:
        return 409, "重放进行中，不能改重放设置"
    replaying = conn.execute(
        "SELECT COUNT(*) AS n FROM reject_rows WHERE status = 'replaying'"
    ).fetchone()["n"]
    if replaying:
        return 409, "重放进行中，不能改重放设置"
    conn.execute(
        """
        UPDATE replay_settings
        SET required_disposition = %s, allow_edit = %s, updated_by = %s, updated_at = NOW()
        WHERE id = 1
        """,
        (required_disposition, allow_edit, username),
    )
    return 200, get_settings(conn)


def queue_row(conn, row_id: int):
    row = conn.execute(
        "SELECT id, status FROM reject_rows WHERE id = %s FOR UPDATE", (row_id,)
    ).fetchone()
    if not row:
        return 404, "没有这一行未接纳报文"
    if row["status"] == "queued":
        return 200, {"id": row_id, "status": "queued"}
    if row["status"] != "active":
        return 409, "这一行不在未接纳状态，不能加入待重放队列"
    conn.execute("UPDATE reject_rows SET status = 'queued' WHERE id = %s", (row_id,))
    return 200, {"id": row_id, "status": "queued"}


def dequeue_row(conn, row_id: int):
    row = conn.execute(
        "SELECT id, status FROM reject_rows WHERE id = %s FOR UPDATE", (row_id,)
    ).fetchone()
    if not row:
        return 404, "没有这一行未接纳报文"
    if row["status"] == "active":
        return 200, {"id": row_id, "status": "active"}
    if row["status"] != "queued":
        return 409, "这一行正在重放或已经处理，不能撤出队列"
    conn.execute("UPDATE reject_rows SET status = 'active' WHERE id = %s", (row_id,))
    return 200, {"id": row_id, "status": "active"}


def set_disposition(conn, row_id: int, disposition: str, username: str):
    if disposition not in DISPOSITIONS:
        return 400, "去向只能是 keep 或 replace"
    row = conn.execute(
        """
        SELECT id, status, disposition, disposition_date
        FROM reject_rows WHERE id = %s FOR UPDATE
        """,
        (row_id,),
    ).fetchone()
    if not row:
        return 404, "没有这一行未接纳报文"
    if row["status"] not in OPEN_STATUSES:
        return 409, "这一行已经重放完成或已撤下，去向不可改"
    today = conn.execute(
        "SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date AS d"
    ).fetchone()["d"]
    if row["disposition"] == disposition:
        return 200, {"id": row_id, "disposition": disposition}
    if row["disposition"] and row["disposition_date"] == today:
        return 409, "重放去向当天已冻结，不能回调"
    conn.execute(
        """
        UPDATE reject_rows
        SET disposition = %s, disposition_date = %s, disposition_by = %s
        WHERE id = %s
        """,
        (disposition, today, username, row_id),
    )
    return 200, {
        "id": row_id,
        "disposition": disposition,
        "disposition_label": DISPOSITIONS[disposition],
    }


def _insert_attempt(conn, **fields):
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["%s"] * len(fields))
    return conn.execute(
        f"INSERT INTO replay_attempts ({columns}) VALUES ({placeholders}) RETURNING id",
        tuple(fields.values()),
    ).fetchone()["id"]


def _row_lock_key(reject_row_id: int) -> int:
    return (REPLAY_ROW_NAMESPACE << 32) | (int(reject_row_id) & 0xFFFFFFFF)


def replay_row(conn, row_id: int, username: str, raw_line: str | None = None):
    # Claim the row for the whole replay with a session advisory lock.  A
    # second person replaying the same row fails this try immediately and is
    # recorded as 重放争用 instead of queueing behind the first claim.
    claim_key = _row_lock_key(row_id)
    claimed = conn.execute(
        "SELECT pg_try_advisory_lock(%s) AS ok", (claim_key,)
    ).fetchone()["ok"]

    # Shared lock lets several replays run at once but keeps the replay
    # settings frozen for the duration.
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (REPLAY_LOCK_KEY,))

    try:
        head = conn.execute(
            "SELECT batch_id FROM reject_rows WHERE id = %s", (row_id,)
        ).fetchone()
        if not head:
            return 404, "没有这一行未接纳报文"
        # Serialise line-number allocation within the batch.
        conn.execute(
            "SELECT id FROM intake_batches WHERE id = %s FOR UPDATE", (head["batch_id"],)
        )

        row = conn.execute(
            "SELECT * FROM reject_rows WHERE id = %s FOR UPDATE", (row_id,)
        ).fetchone()
        if not row:
            return 404, "没有这一行未接纳报文"

        settings = get_settings(conn)

        if not claimed:
            _insert_attempt(
                conn,
                batch_id=row["batch_id"],
                reject_row_id=row["id"],
                original_line_no=row["line_no"],
                new_line_no=None,
                outcome="重放争用",
                disposition=row["disposition"],
                disposition_label=DISPOSITIONS.get(row["disposition"]),
                mismatch=bool(row["disposition"])
                and row["disposition"] != settings["required_disposition"],
                mismatch_note="重放去向不符"
                if row["disposition"] and row["disposition"] != settings["required_disposition"]
                else None,
                original_reason=row["reason"],
                replay_reason="重放争用：这一行已被另一次认领",
                failure_count=row["failure_count"],
                attempted_raw=row["raw_line"],
                attempted_by=username,
            )
            return 409, "重放争用：这一行已被另一次认领，本次不重复重放"

        if row["status"] != "queued":
            return 409, "这一行不在待重放队列"

        return _replay_claimed_row(conn, row, settings, username, raw_line)

    finally:
        if claimed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (claim_key,))


def _replay_claimed_row(conn, row, settings, username, raw_line):
    from parser import parse_telegram_line

    if not row["disposition"]:
        return 400, "请先选定重放去向"

    attempted = row["raw_line"]
    if raw_line is not None and raw_line.strip() and raw_line.strip() != row["raw_line"]:
        if not settings["allow_edit"]:
            return 400, "重放设置不允许改行"
        attempted = raw_line.strip()

    mismatch = row["disposition"] != settings["required_disposition"]
    disposition_label = DISPOSITIONS.get(row["disposition"])

    conn.execute(
        """
        UPDATE reject_rows
        SET status = 'replaying', claimed_by = %s, claimed_at = NOW()
        WHERE id = %s
        """,
        (username, row["id"]),
    )

    original_reason = row["reason"]
    parsed, reason = parse_telegram_line(attempted)
    if not reason:
        existing = conn.execute(
            "SELECT id FROM landed_rows WHERE station_code = %s AND observed_at = %s",
            (parsed["station_code"], parsed["observed_at"]),
        ).fetchone()
        if existing:
            reason = "这个站这个时刻已经落过库"

    new_line_no = None
    if not reason:
        new_line_no = _next_line_no(conn, row["batch_id"])
        try:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO landed_rows
                        (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag, origin, source_reject_id)
                    VALUES (%s, %s, %s, %s, %s, %s, 'replay', %s)
                    """,
                    (
                        row["batch_id"],
                        new_line_no,
                        parsed["station_code"],
                        parsed["observed_at"],
                        parsed["tide_cm"],
                        parsed["quality_flag"],
                        row["id"],
                    ),
                )
        except UniqueViolation:
            # Someone else landed the same station+time first; the replay
            # still failed and the row must not stay stuck in 重放中.
            reason = "这个站这个时刻已经落过库"
            new_line_no = None

    if reason:
        failure_count = row["failure_count"] + 1
        entry = _reason_entry(reason, "replay", username)
        new_line_no = None
        if row["disposition"] == "replace":
            new_line_no = _next_line_no(conn, row["batch_id"])
            new_id = _insert_reject(
                conn,
                row["batch_id"],
                new_line_no,
                attempted,
                reason,
                origin="replay",
                source_reject_id=row["id"],
            )
            conn.execute(
                """
                UPDATE reject_rows
                SET status = 'withdrawn', failure_count = %s, claimed_by = NULL,
                    claimed_at = NULL, replaced_by_id = %s
                WHERE id = %s
                """,
                (failure_count, new_id, row["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE reject_rows
                SET status = 'active', failure_count = %s, claimed_by = NULL,
                    claimed_at = NULL
                WHERE id = %s
                """,
                (failure_count, row["id"]),
            )
        _append_reason(conn, row["id"], entry)
        attempt_id = _insert_attempt(
            conn,
            batch_id=row["batch_id"],
            reject_row_id=row["id"],
            original_line_no=row["line_no"],
            new_line_no=new_line_no,
            outcome="重放失败",
            disposition=row["disposition"],
            disposition_label=disposition_label,
            mismatch=mismatch,
            mismatch_note="重放去向不符" if mismatch else None,
            original_reason=original_reason,
            replay_reason=reason,
            failure_count=failure_count,
            attempted_raw=attempted,
            attempted_by=username,
        )
        _recompute_batch(conn, row["batch_id"])
        return 200, {
            "outcome": "failed",
            "attempt_id": attempt_id,
            "reason": reason,
            "new_line_no": new_line_no,
            "failure_count": failure_count,
            "disposition": row["disposition"],
            "disposition_label": disposition_label,
            "mismatch": mismatch,
            "mismatch_note": "重放去向不符" if mismatch else None,
        }

    conn.execute(
        """
        UPDATE reject_rows
        SET status = 'replayed', claimed_by = NULL, claimed_at = NULL
        WHERE id = %s
        """,
        (row["id"],),
    )
    attempt_id = _insert_attempt(
        conn,
        batch_id=row["batch_id"],
        reject_row_id=row["id"],
        original_line_no=row["line_no"],
        new_line_no=new_line_no,
        outcome="重放成功",
        disposition=row["disposition"],
        disposition_label=disposition_label,
        mismatch=mismatch,
        mismatch_note="重放去向不符" if mismatch else None,
        original_reason=original_reason,
        replay_reason=None,
        failure_count=row["failure_count"],
        attempted_raw=attempted,
        attempted_by=username,
    )
    _recompute_batch(conn, row["batch_id"])
    return 200, {
        "outcome": "success",
        "attempt_id": attempt_id,
        "new_line_no": new_line_no,
        "failure_count": row["failure_count"],
        "disposition": row["disposition"],
        "disposition_label": disposition_label,
        "mismatch": mismatch,
        "mismatch_note": "重放去向不符" if mismatch else None,
    }


def list_replays(conn, batch_id: int | None = None):
    sql = """
        SELECT a.*, b.title AS batch_title
        FROM replay_attempts a
        JOIN intake_batches b ON b.id = a.batch_id
    """
    params = ()
    if batch_id is not None:
        sql += " WHERE a.batch_id = %s"
        params = (batch_id,)
    sql += " ORDER BY a.id DESC"
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        row["attempted_at"] = row["attempted_at"].isoformat(sep=" ", timespec="minutes")
    return rows


def list_queue(conn):
    rows = conn.execute(
        """
        SELECT r.id, r.batch_id, r.line_no, r.raw_line, r.reason, r.failure_count,
               r.disposition, r.status, r.origin, b.title AS batch_title
        FROM reject_rows r
        JOIN intake_batches b ON b.id = r.batch_id
        WHERE r.status = 'queued'
        ORDER BY r.id
        """
    ).fetchall()
    for row in rows:
        row["disposition_label"] = DISPOSITIONS.get(row["disposition"])
    return rows


def _seed(conn):
    title, _, body = SEED_CLEAN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin")
    title, _, body = SEED_BROKEN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin")


SEED_CLEAN = """吴淞口 11-12 早班
WG01|202611120830|186|A
WG01|202611120900|191|A
BG02|202611120830|142|M
"""

SEED_BROKEN = """外高桥 11-12 错报
LG04|202611121000|205|A
WG01|202611120830|186|A
WG0X|202611121000|10|A
"""
