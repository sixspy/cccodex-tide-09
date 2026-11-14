from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from auth import authenticate, issue_token, read_user
from db import (
    DISPOSITIONS,
    connect,
    dequeue_row,
    get_settings,
    init_db,
    list_queue,
    list_replays,
    queue_row,
    replay_row,
    set_disposition,
    update_settings,
)
from parser import FLAGS


class LoginBody(BaseModel):
    username: str
    password: str


class BatchBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    raw_text: str = Field(min_length=1)


class SettingsBody(BaseModel):
    required_disposition: str
    allow_edit: bool = True


class DispositionBody(BaseModel):
    disposition: str


class ReplayBody(BaseModel):
    raw_line: str | None = None


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def _user_or_401(authorization: str | None):
    user = read_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _writer_or_403(authorization: str | None):
    user = _user_or_401(authorization)
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="档案员不能重放报文")
    return user


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "tide-telegram-intake"}


@app.post("/api/login")
def login(body: LoginBody):
    user = authenticate(body.username.strip(), body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": issue_token(user),
        "username": user["username"],
        "role": user["role"],
        "title": user["title"],
    }


@app.get("/api/batches")
def list_batches(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, accepted_count, rejected_count, created_by, created_at
            FROM intake_batches
            ORDER BY id DESC
            """
        ).fetchall()
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
    return rows


def _batch_payload(conn, batch_id: int):
    batch = conn.execute(
        """
        SELECT id, title, raw_text, status, accepted_count, rejected_count, created_by, created_at
        FROM intake_batches WHERE id = %s
        """,
        (batch_id,),
    ).fetchone()
    if not batch:
        raise HTTPException(status_code=404, detail="没有这一批报文")
    landed = conn.execute(
        """
        SELECT id, line_no, station_code, observed_at, tide_cm, quality_flag, origin, source_reject_id
        FROM landed_rows WHERE batch_id = %s ORDER BY line_no
        """,
        (batch_id,),
    ).fetchall()
    rejects = conn.execute(
        """
        SELECT r.id, r.batch_id, r.line_no, r.raw_line, r.reason, r.reasons, r.status, r.failure_count,
               r.disposition, r.disposition_date, r.disposition_by, r.replaced_by_id,
               r.origin, r.source_reject_id, r.claimed_by,
               src.line_no AS source_line_no,
               dst.line_no AS replaced_by_line_no
        FROM reject_rows r
        LEFT JOIN reject_rows src ON src.id = r.source_reject_id
        LEFT JOIN reject_rows dst ON dst.id = r.replaced_by_id
        WHERE r.batch_id = %s
        ORDER BY r.line_no, r.id
        """,
        (batch_id,),
    ).fetchall()
    attempts = conn.execute(
        """
        SELECT id, reject_row_id, original_line_no, new_line_no, outcome,
               disposition, disposition_label, mismatch, mismatch_note,
               original_reason, replay_reason, failure_count,
               attempted_by, attempted_at
        FROM replay_attempts
        WHERE batch_id = %s
        ORDER BY id DESC
        """,
        (batch_id,),
    ).fetchall()

    attempts_by_row = {}
    for attempt in attempts:
        attempts_by_row.setdefault(attempt["reject_row_id"], []).append(attempt["id"])

    batch["created_at"] = _stamp(batch["created_at"])
    for row in landed:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
        row["replayed"] = row["origin"] == "replay"
    for row in rejects:
        row["disposition_label"] = DISPOSITIONS.get(row["disposition"])
        row["disposition_date"] = (
            row["disposition_date"].isoformat() if row["disposition_date"] else None
        )
        row["replayed"] = row["status"] == "replayed"
        row["withdrawn"] = row["status"] == "withdrawn"
        row["queued"] = row["status"] == "queued"
        row["replaying"] = row["status"] == "replaying"
        row["attempt_ids"] = attempts_by_row.get(row["id"], [])
        row["history"] = row["reasons"] or []
        row.pop("reasons", None)
    for attempt in attempts:
        attempt["attempted_at"] = _stamp(attempt["attempted_at"])
        attempt["current"] = None
        attempt["original_label"] = "首次原因"
        attempt["replay_label"] = "重放原因"
    return {"batch": batch, "landed": landed, "rejected": rejects, "replays": attempts}


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        return _batch_payload(conn, batch_id)


@app.post("/api/batches")
def create_batch(body: BatchBody, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    from db import _insert_batch

    title = body.title.strip()
    raw_text = body.raw_text.strip()
    if not title or not raw_text:
        raise HTTPException(status_code=400, detail="标题和报文都不能空")
    with connect() as conn:
        batch_id = _insert_batch(conn, title, raw_text, user["username"])
        conn.commit()
        return _batch_payload(conn, batch_id)


@app.get("/api/replay-settings")
def replay_settings(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        return get_settings(conn)


@app.put("/api/replay-settings")
def save_replay_settings(
    body: SettingsBody, authorization: str | None = Header(default=None)
):
    user = _writer_or_403(authorization)
    if body.required_disposition not in DISPOSITIONS:
        raise HTTPException(status_code=400, detail="去向只能是 keep 或 replace")
    with connect() as conn:
        code, payload = update_settings(
            conn, user["username"], body.required_disposition, body.allow_edit
        )
        conn.commit()
    if code != 200:
        raise HTTPException(status_code=code, detail=payload)
    return payload


@app.get("/api/replay-queue")
def replay_queue(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        return list_queue(conn)


@app.get("/api/replays")
def replays(batch_id: int | None = None, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        return list_replays(conn, batch_id)


@app.post("/api/rejects/{row_id}/queue")
def enqueue_reject(row_id: int, authorization: str | None = Header(default=None)):
    _writer_or_403(authorization)
    with connect() as conn:
        code, payload = queue_row(conn, row_id)
        conn.commit()
    if code != 200:
        raise HTTPException(status_code=code, detail=payload)
    return payload


@app.delete("/api/rejects/{row_id}/queue")
def dequeue_reject(row_id: int, authorization: str | None = Header(default=None)):
    _writer_or_403(authorization)
    with connect() as conn:
        code, payload = dequeue_row(conn, row_id)
        conn.commit()
    if code != 200:
        raise HTTPException(status_code=code, detail=payload)
    return payload


@app.put("/api/rejects/{row_id}/disposition")
def choose_disposition(
    row_id: int, body: DispositionBody, authorization: str | None = Header(default=None)
):
    user = _writer_or_403(authorization)
    if body.disposition not in DISPOSITIONS:
        raise HTTPException(status_code=400, detail="去向只能是 keep 或 replace")
    with connect() as conn:
        code, payload = set_disposition(conn, row_id, body.disposition, user["username"])
        conn.commit()
    if code != 200:
        raise HTTPException(status_code=code, detail=payload)
    return payload


@app.post("/api/rejects/{row_id}/replay")
def run_replay(
    row_id: int, body: ReplayBody, authorization: str | None = Header(default=None)
):
    user = _writer_or_403(authorization)
    with connect() as conn:
        code, payload = replay_row(conn, row_id, user["username"], body.raw_line)
        conn.commit()
    if code != 200:
        raise HTTPException(status_code=code, detail=payload)
    return payload


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
