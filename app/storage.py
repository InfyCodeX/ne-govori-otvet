"""Хранение сессий и учебного прогресса в SQLite."""

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.submissions import normalize


def event_key(update: dict[str, Any]) -> str:
    callback = update.get("callback")
    if update.get("update_type") == "message_callback" and isinstance(callback, dict):
        callback_id = callback.get("callback_id")
        if isinstance(callback_id, str) and callback_id:
            return hashlib.sha256(f"callback:{callback_id}".encode()).hexdigest()
    message = update.get("message")
    if update.get("update_type") == "message_created" and isinstance(message, dict):
        body, recipient = message.get("body"), message.get("recipient")
        if isinstance(body, dict) and isinstance(body.get("mid"), str) and body["mid"]:
            chat_id = recipient.get("chat_id") if isinstance(recipient, dict) else None
            material = json.dumps(["message_created", chat_id, body["mid"]])
        else:
            material = json.dumps(update, sort_keys=True, ensure_ascii=False)
    else:
        material = json.dumps(update, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                key TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN
                    ('pending', 'sent', 'skipped', 'failed', 'uncertain')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS learning_sessions (
                learner TEXT PRIMARY KEY, state TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS skill_progress (
                learner TEXT NOT NULL, skill TEXT NOT NULL,
                mistakes INTEGER NOT NULL DEFAULT 0,
                corrections INTEGER NOT NULL DEFAULT 0,
                solved INTEGER NOT NULL DEFAULT 0,
                hints INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (learner, skill)
            );
            CREATE TABLE IF NOT EXISTS learning_topics (
                learner TEXT NOT NULL, skill TEXT NOT NULL,
                PRIMARY KEY (learner, skill)
            );
            CREATE TABLE IF NOT EXISTS learning_exercises (
                learner TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY (learner, digest)
            );
            CREATE TABLE IF NOT EXISTS ai_usage (
                day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ai_budget (
                id TEXT PRIMARY KEY,
                reserved_kopecks INTEGER NOT NULL CHECK(reserved_kopecks > 0),
                charged_kopecks INTEGER CHECK(charged_kopecks >= 0)
            );
            CREATE TABLE IF NOT EXISTS learner_ai_usage (
                day TEXT NOT NULL, learner TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, learner)
            );
            CREATE TABLE IF NOT EXISTS learning_observations (
                learner TEXT NOT NULL, skill TEXT NOT NULL, metric TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS observations_learner_time
                ON learning_observations(learner, created_at);
            """
        )
        self.connection.commit()
        self.expire_learning_sessions()
        with self.connection:
            self.connection.execute(
                "DELETE FROM learning_observations WHERE created_at < datetime('now', '-90 days')"
            )
            self.connection.execute(
                "DELETE FROM learner_ai_usage WHERE day < date('now', '-7 days')"
            )

    def close(self) -> None:
        self.connection.close()

    def bind_bot(self, bot_id: int) -> None:
        existing = self._get("bot_id")
        if existing is not None and existing != str(bot_id):
            raise RuntimeError("База уже связана с другим ботом: выберите другой DATABASE_PATH")
        self._set("bot_id", str(bot_id))

    def _get(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def marker(self) -> int | None:
        value = self._get("marker")
        return int(value) if value is not None else None

    def save_marker(self, marker: int | None) -> None:

        if marker is not None:
            self._set("marker", str(marker))

    def reserve(self, key: str) -> bool:
        with self.connection:
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO events(key, status) VALUES (?, 'pending')", (key,)
            ).rowcount
            if not inserted:
                self.connection.execute(
                    "UPDATE events SET status = 'uncertain' WHERE key = ? AND status = 'pending'",
                    (key,),
                )
        return bool(inserted)

    def finish(self, key: str, status: str) -> None:
        if status not in {"sent", "skipped", "failed", "uncertain"}:
            raise ValueError("Invalid terminal event status")
        with self.connection:
            self.connection.execute("UPDATE events SET status = ? WHERE key = ?", (status, key))

    def status(self, key: str) -> str | None:
        row = self.connection.execute("SELECT status FROM events WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def expire_learning_sessions(self) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM learning_sessions WHERE updated_at < datetime('now', '-7 days')"
            )

    def learning_session(self, learner: str) -> dict[str, Any] | None:
        self.expire_learning_sessions()
        row = self.connection.execute(
            "SELECT state FROM learning_sessions WHERE learner = ?", (learner,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_learning_session(self, learner: str, state: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO learning_sessions(learner, state) VALUES (?, ?) "
                "ON CONFLICT(learner) DO UPDATE SET state=excluded.state, "
                "updated_at=CURRENT_TIMESTAMP",
                (learner, json.dumps(state, ensure_ascii=False)),
            )

    def forget_learning(self, learner: str, *, progress: bool = False) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM learning_sessions WHERE learner = ?", (learner,))
            if progress:
                self.connection.execute("DELETE FROM learning_topics WHERE learner = ?", (learner,))
                self.connection.execute(
                    "DELETE FROM learning_exercises WHERE learner = ?", (learner,)
                )
                self.connection.execute("DELETE FROM skill_progress WHERE learner = ?", (learner,))
                self.connection.execute(
                    "DELETE FROM learning_observations WHERE learner = ?", (learner,)
                )

    def record_progress(self, learner: str, skill: str, metric: str) -> None:
        if metric not in {"mistakes", "corrections", "solved", "hints"}:
            raise ValueError("Unknown progress metric")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO skill_progress(learner, skill) VALUES (?, ?)",
                (learner, skill),
            )
            self.connection.execute(
                f"UPDATE skill_progress SET {metric} = {metric} + 1 "
                "WHERE learner = ? AND skill = ?",
                (learner, skill),
            )
            self.connection.execute(
                "INSERT INTO learning_observations(learner, skill, metric) VALUES (?, ?, ?)",
                (learner, skill, metric),
            )

    def learning_progress(self, learner: str) -> list[tuple]:
        return self.connection.execute(
            "SELECT skill, mistakes, corrections, solved, hints FROM skill_progress "
            "WHERE learner = ? ORDER BY mistakes DESC, skill",
            (learner,),
        ).fetchall()

    def learning_topics(self, learner: str) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT skill FROM learning_topics WHERE learner = ? ORDER BY skill", (learner,)
            )
        ]

    def register_topic(self, learner: str, skill: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO learning_topics(learner, skill) VALUES (?, ?)",
                (learner, skill),
            )

    def remember_exercise(self, learner: str, problem: str) -> bool:
        digest = hashlib.sha256(normalize(problem).encode()).hexdigest()
        with self.connection:
            return (
                self.connection.execute(
                    "INSERT OR IGNORE INTO learning_exercises(learner, digest) VALUES (?, ?)",
                    (learner, digest),
                ).rowcount
                == 1
            )

    def exercise_seen(self, learner: str, problem: str) -> bool:
        digest = hashlib.sha256(normalize(problem).encode()).hexdigest()
        return (
            self.connection.execute(
                "SELECT 1 FROM learning_exercises WHERE learner = ? AND digest = ?",
                (learner, digest),
            ).fetchone()
            is not None
        )

    def learning_dynamics(self, learner: str) -> list[tuple]:

        return self.connection.execute(
            "SELECT skill, "
            "SUM(CASE WHEN metric='mistakes' AND created_at >= datetime('now','-7 days') "
            "THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN metric='corrections' AND created_at >= datetime('now','-7 days') "
            "THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN metric='mistakes' AND created_at < datetime('now','-7 days') "
            "THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN metric='corrections' AND created_at < datetime('now','-7 days') "
            "THEN 1 ELSE 0 END) "
            "FROM learning_observations WHERE learner = ? "
            "AND created_at >= datetime('now','-14 days') GROUP BY skill",
            (learner,),
        ).fetchall()

    def reserve_ai_call(
        self, daily_limit: int, learner: str | None = None, learner_limit: int = 20
    ) -> bool:
        if daily_limit <= 0:
            return False
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO ai_usage(day) VALUES (date('now'))")
            if learner is not None:
                self.connection.execute(
                    "INSERT OR IGNORE INTO learner_ai_usage(day, learner) VALUES (date('now'), ?)",
                    (learner,),
                )
                used = self.connection.execute(
                    "SELECT requests FROM learner_ai_usage WHERE day=date('now') AND learner=?",
                    (learner,),
                ).fetchone()[0]
                if used >= learner_limit:
                    return False
            changed = self.connection.execute(
                "UPDATE ai_usage SET requests = requests + 1 "
                "WHERE day = date('now') AND requests < ?",
                (daily_limit,),
            ).rowcount
            if changed and learner is not None:
                self.connection.execute(
                    "UPDATE learner_ai_usage SET requests=requests+1 "
                    "WHERE day=date('now') AND learner=?",
                    (learner,),
                )
        return bool(changed)

    def reserve_ai_budget(self, limit_kopecks: int, amount_kopecks: int = 5500) -> str | None:

        if type(limit_kopecks) is not int or type(amount_kopecks) is not int:
            raise ValueError("Budget values must be integer kopecks")
        if limit_kopecks <= 0 or amount_kopecks <= 0:
            return None
        reservation = uuid.uuid4().hex
        with self.connection:
            inserted = self.connection.execute(
                "INSERT INTO ai_budget(id, reserved_kopecks) "
                "SELECT ?, ? WHERE "
                "(SELECT COALESCE(SUM(COALESCE(charged_kopecks, reserved_kopecks)), 0) "
                "FROM ai_budget) + ? <= ?",
                (reservation, amount_kopecks, amount_kopecks, limit_kopecks),
            ).rowcount
        return reservation if inserted else None

    def settle_ai_budget(self, reservation: str, charged_kopecks: int) -> None:

        if type(charged_kopecks) is not int or charged_kopecks < 0:
            raise ValueError("Cost must be nonnegative integer kopecks")
        with self.connection:
            self.connection.execute(
                "UPDATE ai_budget SET charged_kopecks=? WHERE id=? AND charged_kopecks IS NULL",
                (charged_kopecks, reservation),
            )

    def ai_budget_used_kopecks(self) -> int:
        return self.connection.execute(
            "SELECT COALESCE(SUM(COALESCE(charged_kopecks, reserved_kopecks)), 0) FROM ai_budget"
        ).fetchone()[0]

    def ai_budget_totals(self) -> tuple[int, int]:

        row = self.connection.execute(
            "SELECT COALESCE(SUM(charged_kopecks),0), "
            "COALESCE(SUM(CASE WHEN charged_kopecks IS NULL THEN reserved_kopecks ELSE 0 END),0) "
            "FROM ai_budget"
        ).fetchone()
        return row[0], row[1]
