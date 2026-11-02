"""Локальная память: история разговора, факты о пользователе и таймеры (SQLite)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS timers (id INTEGER PRIMARY KEY, label TEXT NOT NULL, due REAL NOT NULL, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS activity (day TEXT NOT NULL, kind TEXT NOT NULL, app TEXT NOT NULL, seconds REAL NOT NULL, PRIMARY KEY (day, kind, app));
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS told_jokes (key TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS routines (name TEXT PRIMARY KEY, steps TEXT NOT NULL, ts REAL NOT NULL);
"""
MAX_FACT = 240
MAX_FACTS = 60
MAX_MESSAGES = 2000
MAX_NOTE = 400
MAX_NOTES = 200
ACTIVITY_KEEP_DAYS = 120


class Memory:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(timers)")}
        if "repeat" not in columns:
            # База могла остаться от первой версии Джарвис: таблицу не пересоздаём, таймеры должны сохраниться.
            self.db.execute("ALTER TABLE timers ADD COLUMN repeat TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def add_message(self, role: str, content: str) -> None:
        self.db.execute("INSERT INTO messages(role, content, ts) VALUES (?, ?, ?)", (role, content, time.time()))
        self.db.execute(
            "DELETE FROM messages WHERE id <= (SELECT MAX(id) FROM messages) - ?", (MAX_MESSAGES,)
        )
        self.db.commit()

    def recent_messages(self, limit: int = 12, max_age_hours: float = 8, char_budget: int = 2000) -> list[dict[str, str]]:
        """Недавний контекст. Бюджет символов защищает минутный лимит токенов бесплатного тарифа."""
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE ts > ? ORDER BY id DESC LIMIT ?",
            (time.time() - max_age_hours * 3600, limit),
        ).fetchall()
        picked: list[dict[str, str]] = []
        used = 0
        for row in rows:
            used += len(row["content"])
            if used > char_budget and picked:
                break
            picked.append({"role": row["role"], "content": row["content"]})
        picked.reverse()
        # Ответ модели не должен идти первым: некоторые API считают такую историю ошибкой.
        while picked and picked[0]["role"] != "user":
            picked.pop(0)
        return picked

    def retire_history(self) -> None:
        """Записи остаются, но больше не попадают в контекст модели: старые ответы могли быть плохим примером."""
        self.db.execute("UPDATE messages SET ts = 0")
        self.db.commit()

    def clear_history(self) -> None:
        self.db.execute("DELETE FROM messages")
        self.db.commit()

    def routines(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT name, steps FROM routines ORDER BY ts").fetchall()
        return [{"name": row["name"], "steps": json.loads(row["steps"])} for row in rows]

    def routine(self, name: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT name, steps FROM routines WHERE name = ?", (name,)).fetchone()
        return {"name": row["name"], "steps": json.loads(row["steps"])} if row else None

    def save_routine(self, name: str, steps: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO routines(name, steps, ts) VALUES (?, ?, ?)",
                        (name, json.dumps(steps, ensure_ascii=False), time.time()))
        self.db.commit()

    def delete_routine(self, name: str) -> bool:
        deleted = self.db.execute("DELETE FROM routines WHERE name = ?", (name,)).rowcount
        self.db.commit()
        return deleted > 0

    def add_fact(self, text: str) -> bool:
        text = " ".join(text.split())[:MAX_FACT]
        if not text:
            return False
        self.db.execute("INSERT OR REPLACE INTO facts(text, ts) VALUES (?, ?)", (text, time.time()))
        self.db.execute(
            "DELETE FROM facts WHERE id NOT IN (SELECT id FROM facts ORDER BY ts DESC LIMIT ?)", (MAX_FACTS,)
        )
        self.db.commit()
        return True

    def facts(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT id, text FROM facts ORDER BY ts DESC").fetchall()
        return [dict(row) for row in rows]

    def delete_fact(self, fact_id: int) -> None:
        self.db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self.db.commit()

    def forget(self, query: str) -> int:
        needle = query.strip().lower()
        if not needle:
            return 0
        # SQLite приводит к нижнему регистру только латиницу, поэтому кириллицу сравниваем в Python.
        victims = [row["id"] for row in self.facts() if needle in row["text"].lower()]
        for fact_id in victims:
            self.delete_fact(fact_id)
        return len(victims)

    def add_timer(self, label: str, due: float, repeat: str = "") -> int:
        cur = self.db.execute("INSERT INTO timers(label, due, repeat) VALUES (?, ?, ?)", (label[:120], due, repeat))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def active_timers(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT id, label, due, repeat FROM timers WHERE done = 0 ORDER BY due").fetchall()
        return [dict(row) for row in rows]

    def finish_timer(self, timer_id: int) -> None:
        self.db.execute("UPDATE timers SET done = 1 WHERE id = ?", (timer_id,))
        self.db.commit()

    def reschedule_timer(self, timer_id: int, due: float) -> None:
        self.db.execute("UPDATE timers SET due = ? WHERE id = ?", (due, timer_id))
        self.db.commit()

    # ---------- Заметки и дела ----------

    def add_note(self, text: str) -> int:
        text = " ".join(text.split())[:MAX_NOTE]
        if not text:
            return 0
        cur = self.db.execute("INSERT INTO notes(text, ts) VALUES (?, ?)", (text, time.time()))
        self.db.execute(
            "DELETE FROM notes WHERE id NOT IN (SELECT id FROM notes ORDER BY ts DESC LIMIT ?)", (MAX_NOTES,)
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def notes(self, include_done: bool = False) -> list[dict[str, Any]]:
        where = "" if include_done else "WHERE done = 0"
        rows = self.db.execute(f"SELECT id, text, done, ts FROM notes {where} ORDER BY ts").fetchall()  # noqa: S608
        return [dict(row) for row in rows]

    def set_note_done(self, note_id: int, done: bool = True) -> None:
        self.db.execute("UPDATE notes SET done = ? WHERE id = ?", (int(done), note_id))
        self.db.commit()

    def delete_note(self, note_id: int) -> None:
        self.db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.db.commit()

    # ---------- Рассказанные шутки ----------

    def told_jokes(self) -> set[str]:
        return {row["key"] for row in self.db.execute("SELECT key FROM told_jokes")}

    def remember_joke(self, key: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO told_jokes(key, ts) VALUES (?, ?)", (key, time.time()))
        # Через пару сотен шуток старые можно рассказывать снова: все уже забыли.
        self.db.execute("DELETE FROM told_jokes WHERE key NOT IN (SELECT key FROM told_jokes ORDER BY ts DESC LIMIT 200)")
        self.db.commit()

    # ---------- Экранное время ----------

    def add_activity(self, day: str, kind: str, app: str, seconds: float) -> None:
        self.db.execute(
            "INSERT INTO activity(day, kind, app, seconds) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day, kind, app) DO UPDATE SET seconds = seconds + excluded.seconds",
            (day, kind, app[:60], seconds),
        )
        self.db.execute(
            "DELETE FROM activity WHERE day < date('now', ?)", (f"-{ACTIVITY_KEEP_DAYS} days",)
        )
        self.db.commit()

    def activity_for(self, day: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT kind, app, seconds FROM activity WHERE day = ?", (day,)).fetchall()
        return [dict(row) for row in rows]

    def cancel_timers(self, label: str = "") -> int:
        needle = label.strip().lower()
        victims = [t["id"] for t in self.active_timers() if not needle or needle in t["label"].lower()]
        for timer_id in victims:
            self.finish_timer(timer_id)
        return len(victims)
