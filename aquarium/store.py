"""SQLite storage.

- `seconds`: one row per observed second (positions in cm), pruned after a few days.
- `minutes`: additive roll-ups kept forever; history charts and alerts read these.
- `events`: feeding, alerts and calibration, optionally with a snapshot file name.
"""

import json
import sqlite3
from pathlib import Path

GRID = (12, 6)
MINUTE_FIELDS = ("seconds", "lit", "fish_seconds", "speed_sum", "speed_n", "moving_n", "height_sum", "height_n")


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            CREATE TABLE IF NOT EXISTS seconds (
                ts INTEGER PRIMARY KEY, status TEXT NOT NULL, fish INTEGER NOT NULL,
                speed REAL, moving REAL, height REAL, positions TEXT
            );
            CREATE TABLE IF NOT EXISTS minutes (
                ts INTEGER PRIMARY KEY, seconds INTEGER NOT NULL, lit INTEGER NOT NULL,
                fish_seconds INTEGER NOT NULL, speed_sum REAL NOT NULL, speed_n INTEGER NOT NULL,
                moving_n INTEGER NOT NULL, height_sum REAL NOT NULL, height_n INTEGER NOT NULL, heat TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, kind TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '{}', snapshot TEXT
            );
            CREATE INDEX IF NOT EXISTS events_time ON events(ts_ms);
            CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts_ms);
            PRAGMA user_version = 1;
        """)

    def close(self):
        self.db.close()

    # writes -----------------------------------------------------------------

    def insert_second(self, ts, status, fish, speed, moving, height, positions):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO seconds VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, status, fish, speed, moving, height, json.dumps(positions) if positions is not None else None),
            )

    def save_minute(self, ts, totals: dict, heat: list[int] | None):
        """Store a minute roll-up, adding to any partial row left by a restart within the same minute."""
        with self.db:
            existing = self.db.execute("SELECT * FROM minutes WHERE ts = ?", (ts,)).fetchone()
            if existing is not None:
                totals = {name: totals[name] + existing[name] for name in MINUTE_FIELDS}
                if existing["heat"]:
                    previous = json.loads(existing["heat"])
                    heat = [a + b for a, b in zip(heat or [0] * len(previous), previous)]
            self.db.execute(
                f"INSERT OR REPLACE INTO minutes VALUES (?, {', '.join('?' * len(MINUTE_FIELDS))}, ?)",
                (ts, *(totals[name] for name in MINUTE_FIELDS), json.dumps(heat) if heat else None),
            )

    def add_event(self, ts_ms, kind, detail=None, snapshot=None) -> int:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO events (ts_ms, kind, detail, snapshot) VALUES (?, ?, ?, ?)",
                (ts_ms, kind, json.dumps(detail or {}, ensure_ascii=False), snapshot),
            )
        return cursor.lastrowid

    def prune_seconds(self, before_ts):
        with self.db:
            self.db.execute("DELETE FROM seconds WHERE ts < ?", (before_ts,))

    # reads ------------------------------------------------------------------

    def has_minutes(self) -> bool:
        return self.db.execute("SELECT 1 FROM minutes LIMIT 1").fetchone() is not None

    def history(self, start, end, step) -> dict:
        sums = ", ".join(f"SUM({name}) AS {name}" for name in MINUTE_FIELDS)
        rows = self.db.execute(
            f"SELECT (ts / ?) * ? AS bucket, {sums} FROM minutes WHERE ts >= ? AND ts < ? GROUP BY bucket ORDER BY bucket",
            (step, step, start, end),
        ).fetchall()
        series = [{"ts_ms": row["bucket"] * 1000, **describe(row, step)} for row in rows]
        totals = self.db.execute(f"SELECT {sums} FROM minutes WHERE ts >= ? AND ts < ?", (start, end)).fetchone()
        heat = [0] * (GRID[0] * GRID[1])
        for (cells,) in self.db.execute(
            "SELECT heat FROM minutes WHERE ts >= ? AND ts < ? AND heat IS NOT NULL", (start, end),
        ):
            for index, count in enumerate(json.loads(cells)):
                heat[index] += count
        summary = describe(totals, end - start)
        summary["observed_s"] = totals["seconds"] or 0
        summary["lit_s"] = totals["lit"] or 0
        return {"series": series, "summary": summary, "heat": {"columns": GRID[0], "rows": GRID[1], "counts": heat}}

    def activity(self, start, end, min_lit) -> tuple[float | None, int]:
        """Mean speed over minutes with enough light, and how many minutes counted."""
        row = self.db.execute(
            "SELECT SUM(speed_sum) AS total, SUM(speed_n) AS n, COUNT(*) AS minutes FROM minutes "
            "WHERE ts >= ? AND ts < ? AND lit >= ? AND speed_n > 0",
            (start, end, min_lit),
        ).fetchone()
        return ratio(row["total"], row["n"]), row["minutes"]

    def window(self, start, end) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) AS lit_s, AVG(speed) AS speed, AVG(moving) AS moving, AVG(height) AS height, "
            "AVG(fish) AS fish FROM seconds WHERE ts >= ? AND ts < ? AND status != 'dark'",
            (start, end),
        ).fetchone()
        return dict(row)

    def events(self, start_ms, end_ms, limit=50, kinds=None) -> list[dict]:
        query = "SELECT * FROM events WHERE ts_ms >= ? AND ts_ms <= ?"
        values = [start_ms, end_ms]
        if kinds:
            query += f" AND kind IN ({', '.join('?' * len(kinds))})"
            values.extend(kinds)
        rows = self.db.execute(query + " ORDER BY ts_ms DESC, id DESC LIMIT ?", (*values, limit)).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def latest_event(self, kind) -> dict | None:
        events = self.events(0, 2**62, 1, [kind])
        return events[0] if events else None


def describe(row, span) -> dict:
    seconds = row["seconds"] or 0
    lit = row["lit"] or 0
    return {
        "coverage": min(1.0, seconds / span) if span else None,
        "lit_ratio": ratio(lit, seconds),
        "fish": ratio(row["fish_seconds"] or 0, lit),
        "speed": ratio(row["speed_sum"] or 0, row["speed_n"]),
        "moving": ratio(row["moving_n"] or 0, row["speed_n"]),
        "height": ratio(row["height_sum"] or 0, row["height_n"]),
    }
