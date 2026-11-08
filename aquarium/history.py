import sqlite3
from itertools import combinations
from math import dist
from pathlib import Path

from .contracts import FEED_POINT, FEED_RADIUS, WIDTH, ObservationFrame, Telemetry

LOW_SPEED = 3.0
FEED_WINDOW_MS = 15_000
RANGES = {"1m": (60_000, 1_000), "15m": (900_000, 10_000), "24h": (86_400_000, 900_000)}


def measure(frame: ObservationFrame, previous: ObservationFrame | None):
    positions = {
        track.id: track.position.coordinates()
        for track in frame.tracks
        if track.status == "tracked" and track.position is not None
    }
    velocities = {}
    if previous and 0 < (delta := frame.timestamp_ms - previous.timestamp_ms) <= 1500:
        for track in previous.tracks:
            if track.status == "tracked" and track.position and track.id in positions:
                velocities[track.id] = tuple(
                    (current - old) * 1000 / delta
                    for current, old in zip(positions[track.id], track.position.coordinates())
                )
    speeds = [dist(velocity, (0, 0, 0)) for velocity in velocities.values()]
    spacing = [dist(left, right) for left, right in combinations(positions.values(), 2)]
    zones = [
        sum(low <= x < high for x, _, _ in positions.values())
        for low, high in ((-WIDTH, -WIDTH / 6), (-WIDTH / 6, WIDTH / 6), (WIDTH / 6, WIDTH))
    ]
    sample = {
        "tracked": len(positions),
        "speed_count": len(speeds),
        "speed_sum": sum(speeds),
        "low_count": sum(speed < LOW_SPEED for speed in speeds),
        "spacing_cm": sum(spacing) / len(spacing) if spacing else None,
        "near_feed": sum(dist(position, FEED_POINT) <= FEED_RADIUS for position in positions.values()),
        "left_count": zones[0],
        "middle_count": zones[1],
        "right_count": zones[2],
    }
    return velocities, sample


class History:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS motion (
                source TEXT, run_id TEXT, timestamp_ms INTEGER,
                tracked INTEGER, speed_count INTEGER, speed_sum REAL, low_count INTEGER,
                spacing_cm REAL, near_feed INTEGER,
                left_count INTEGER, middle_count INTEGER, right_count INTEGER,
                PRIMARY KEY (source, timestamp_ms)
            );
            CREATE INDEX IF NOT EXISTS motion_run ON motion(run_id, timestamp_ms);
            CREATE TABLE IF NOT EXISTS telemetry (
                source TEXT, timestamp_ms INTEGER, temperature_c REAL, transmittance_pct REAL,
                PRIMARY KEY (source, timestamp_ms)
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, source TEXT, run_id TEXT, timestamp_ms INTEGER,
                action TEXT, origin TEXT, enabled INTEGER
            );
            CREATE INDEX IF NOT EXISTS events_source ON events(source, timestamp_ms);
        """)

    def save_motion(self, source, run_id, timestamp_ms, sample):
        with self.db:
            self.db.execute("""
                INSERT INTO motion VALUES (:source, :run_id, :timestamp_ms, :tracked, :speed_count, :speed_sum,
                    :low_count, :spacing_cm, :near_feed, :left_count, :middle_count, :right_count)
                ON CONFLICT(source, timestamp_ms) DO NOTHING
            """, {
                **sample, "source": source, "run_id": run_id, "timestamp_ms": timestamp_ms,
            })

    def save_telemetry(self, source, sample: Telemetry):
        with self.db:
            self.db.execute("""
                INSERT INTO telemetry (source, timestamp_ms, temperature_c, transmittance_pct)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, timestamp_ms) DO NOTHING
            """, (source, sample.timestamp_ms, sample.temperature_c, sample.transmittance_pct))

    def save_event(self, event_id, source, run_id, timestamp_ms, action, origin, enabled):
        with self.db:
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)", (
                event_id, source, run_id, timestamp_ms, action, origin, enabled,
            ))

    def feed_window(self, run_id, start, end):
        row = self.db.execute("""
            SELECT SUM(speed_sum) / NULLIF(SUM(speed_count), 0) AS speed_cm_s,
                AVG(spacing_cm) AS spacing_cm, AVG(near_feed) AS near_feed,
                COUNT(*) AS samples
            FROM motion WHERE run_id = ? AND timestamp_ms >= ? AND timestamp_ms < ? AND tracked > 0
        """, (run_id, start, end)).fetchone()
        return dict(row)

    def read(self, source, run_id, range_name, now_ms):
        span, step = RANGES[range_name]
        start = now_ms - span
        motion = self.db.execute("""
            SELECT timestamp_ms / ? AS bucket, SUM(speed_sum) / NULLIF(SUM(speed_count), 0) AS speed_cm_s,
                AVG(spacing_cm) AS spacing_cm
            FROM motion WHERE source = ? AND timestamp_ms >= ? AND timestamp_ms <= ?
            GROUP BY bucket ORDER BY bucket
        """, (step, source, start, now_ms))
        points = {row["bucket"]: dict(row) for row in motion}
        telemetry = self.db.execute("""
            SELECT timestamp_ms / ? AS bucket, AVG(temperature_c) AS temperature_c,
                AVG(transmittance_pct) AS transmittance_pct
            FROM telemetry WHERE source = ? AND timestamp_ms >= ? AND timestamp_ms <= ?
            GROUP BY bucket ORDER BY bucket
        """, (step, source, start, now_ms))
        for row in telemetry:
            points.setdefault(row["bucket"], {}).update(dict(row))
        series = []
        for bucket in sorted(points):
            point = points[bucket]
            point["timestamp_ms"] = max(start, point.pop("bucket") * step)
            series.append(point)

        summary = self.db.execute("""
            SELECT SUM(speed_sum) / NULLIF(SUM(speed_count), 0) AS speed_cm_s,
                100.0 * SUM(low_count) / NULLIF(SUM(speed_count), 0) AS low_speed_pct,
                SUM(left_count) AS left_count, SUM(middle_count) AS middle_count,
                SUM(right_count) AS right_count
            FROM motion WHERE source = ? AND timestamp_ms BETWEEN ? AND ?
        """, (source, start, now_ms)).fetchone()
        events = self.db.execute("""
            SELECT timestamp_ms, action, origin, enabled FROM events
            WHERE source = ? AND timestamp_ms BETWEEN ? AND ? ORDER BY timestamp_ms DESC LIMIT 8
        """, (source, start, now_ms)).fetchall()
        feed = self.db.execute("""
            SELECT timestamp_ms, origin FROM events WHERE run_id = ? AND action = 'feed'
            ORDER BY timestamp_ms DESC LIMIT 1
        """, (run_id,)).fetchone()
        comparison = None
        if feed:
            timestamp = feed["timestamp_ms"]
            comparison = {
                **dict(feed),
                "before": self.feed_window(run_id, timestamp - FEED_WINDOW_MS, timestamp),
                "after": self.feed_window(run_id, timestamp, timestamp + FEED_WINDOW_MS),
                "collecting": now_ms < timestamp + FEED_WINDOW_MS,
            }
        return {
            "start_ms": start, "end_ms": now_ms, "step_ms": step, "series": series,
            "summary": dict(summary), "events": [dict(row) for row in events], "feed": comparison,
        }
