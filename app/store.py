"""SQLite 저장소: 작업 상태와 확인 기록만 저장한다. API 키는 저장하지 않는다."""
import json
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, token_hash TEXT UNIQUE, created REAL,
              expires REAL, stage TEXT, message TEXT, cap_usd REAL, estimate TEXT);
            CREATE TABLE IF NOT EXISTS records(job_id TEXT, channel_no INTEGER, value TEXT, updated REAL,
              PRIMARY KEY(job_id, channel_no));
            """)
            try:  # 이전 버전 DB에는 없는 열: 단계가 시작된 시각(경과 시간 표시용)
                db.execute("ALTER TABLE jobs ADD COLUMN stage_since REAL")
            except sqlite3.OperationalError:
                pass
            # 서버가 꺼질 때 실행 중이던 작업은 중단 처리
            db.execute("UPDATE jobs SET stage='interrupted', message='서버가 재시작되어 중단됨. 같은 단계를 다시 시작하세요' "
                       "WHERE stage IN ('preparing','analyzing','rechecking')")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return _Ctx(db)

    def create_job(self, job_id, token_hash, ttl_days):
        now = time.time()
        with _lock, self._db() as db:
            db.execute("INSERT INTO jobs(id, token_hash, created, expires, stage, message, cap_usd, estimate, stage_since) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       (job_id, token_hash, now, now + ttl_days * 86400, "created", "", None, None, now))

    def by_token_hash(self, h):
        with self._db() as db:
            r = db.execute("SELECT * FROM jobs WHERE token_hash=?", (h,)).fetchone()
        return dict(r) if r else None

    def update(self, job_id, **fields):
        if "estimate" in fields and not isinstance(fields["estimate"], str):
            fields["estimate"] = json.dumps(fields["estimate"], ensure_ascii=False)
        if "stage" in fields:
            fields["stage_since"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with _lock, self._db() as db:
            db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def set_record(self, job_id, channel_no, value):
        with _lock, self._db() as db:
            db.execute("INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(job_id,channel_no) "
                       "DO UPDATE SET value=excluded.value, updated=excluded.updated",
                       (job_id, channel_no, value, time.time()))

    def records(self, job_id):
        with self._db() as db:
            return {r["channel_no"]: r["value"] for r in
                    db.execute("SELECT channel_no, value FROM records WHERE job_id=?", (job_id,))}

    def delete_job(self, job_id):
        with _lock, self._db() as db:
            db.execute("DELETE FROM records WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def expired(self):
        with self._db() as db:
            return [r["id"] for r in db.execute("SELECT id FROM jobs WHERE expires<?", (time.time(),))]


class _Ctx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.commit()
        self.db.close()
