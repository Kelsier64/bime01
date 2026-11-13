import asyncio
import tempfile
import unittest
from unittest.mock import patch
from math import dist
from pathlib import Path

import cv2
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aquarium.calibration import TankMapper
from aquarium.devices import ManualFeeder
from aquarium.fake import FakeTank, default_calibration, default_settings, seed_history
from aquarium.hub import Catalog
from aquarium.main import create_app
from aquarium.monitor import Monitor
from aquarium.settings import Calibration, Settings, SettingsStore
from aquarium.sources import VideoFile
from aquarium.store import Store
from aquarium.vision import decode

START_MS = 1_800_000_000_000


class Clock:
    def __init__(self, ms=START_MS):
        self.ms = ms

    def __call__(self):
        return self.ms


class Rig:
    """A monitor fed by the fake tank on a simulated clock."""

    def __init__(self, directory: Path, settings: Settings | None = None, fps=10):
        self.clock = Clock()
        self.tank = FakeTank(clock=lambda: self.clock.ms / 1000)
        self.store = Store(directory / "test.sqlite3")
        self.settings = SettingsStore(directory / "settings.json", settings or default_settings())
        self.monitor = Monitor(self.tank, self.store, self.settings, ManualFeeder(self.tank.feed),
                               directory / "snapshots", clock=self.clock, monotonic=lambda: self.clock.ms / 1000)
        self.interval = 1000 // fps

    def run(self, seconds, check=None):
        for _ in range(round(seconds * 1000 / self.interval)):
            self.clock.ms += self.interval
            frame = self.tank.next_frame()
            if frame is not None:
                self.monitor.process(frame)
                if check:
                    check()

    def rows(self, query, *args):
        return self.store.db.execute(query, args).fetchall()


class CalibrationTests(unittest.TestCase):
    def test_corners_map_to_tank_edges_with_height_upwards(self):
        calibration = Calibration(
            corners=((100, 50), (500, 70), (520, 330), (90, 310)),
            frame_width=640, frame_height=360, tank_width_cm=60, tank_height_cm=30,
        )
        mapper = TankMapper(calibration)
        for corner, expected in zip(calibration.corners, [(0, 30), (60, 30), (60, 0), (0, 0)]):
            for value, target in zip(mapper.to_cm(*corner, 640, 360), expected):
                self.assertAlmostEqual(value, target, places=6)
        x, y = mapper.to_cm(100 / 2, 50 / 2, 320, 180)
        self.assertAlmostEqual(x, 0, places=6)
        self.assertAlmostEqual(y, 30, places=6)

    def test_rejects_corners_in_the_wrong_order(self):
        with self.assertRaises(ValidationError):
            Calibration(corners=((0, 0), (0, 100), (100, 100), (100, 0)), frame_width=640, frame_height=360,
                        tank_width_cm=60, tank_height_cm=30)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def rig(self, settings=None):
        rig = Rig(self.directory, settings)
        self.addCleanup(rig.store.close)
        return rig

    def test_tracks_fish_in_centimetres_and_records_speed(self):
        rig = self.rig()
        errors = []

        def check():
            fish = rig.monitor.tracking["tracks"]
            for track in fish:
                errors.append(min(dist((track["x_cm"], track["y_cm"]), (f.x, f.y)) for f in rig.tank.fish))

        true_distance = []
        last = [(f.x, f.y) for f in rig.tank.fish]

        def measure():
            nonlocal last
            check()
            now = [(f.x, f.y) for f in rig.tank.fish]
            true_distance.append(sum(dist(a, b) for a, b in zip(now, last)))
            last = now

        rig.run(65, measure)
        errors.sort()
        self.assertLess(errors[len(errors) // 2], 1.5, "median error well under a body length")
        self.assertLess(errors[-1], 8, "even merged blobs stay within a body length")
        seconds = rig.rows("SELECT * FROM seconds ORDER BY ts")
        self.assertGreaterEqual(len(seconds), 63)
        self.assertTrue(all(row["status"] == "lit" for row in seconds))
        self.assertGreater(sum(row["fish"] == 2 for row in seconds) / len(seconds), 0.8)
        recorded = [row["speed"] for row in seconds if row["speed"] is not None]
        true_speed = sum(true_distance) / 65 / 2
        self.assertAlmostEqual(sum(recorded) / len(recorded), true_speed, delta=true_speed * 0.35)
        minutes = rig.rows("SELECT * FROM minutes")
        self.assertEqual(len(minutes), 1)
        self.assertEqual(minutes[0]["lit"], minutes[0]["seconds"])
        self.assertIsNotNone(minutes[0]["heat"])

    def test_darkness_pauses_tracking_and_is_excluded(self):
        rig = self.rig()
        rig.run(5)
        rig.tank.demo("dark")
        rig.run(5)
        self.assertFalse(rig.monitor.lit)
        self.assertEqual(rig.monitor.tracking["stage"], "dark")
        statuses = [row["status"] for row in rig.rows("SELECT status FROM seconds ORDER BY ts")]
        self.assertEqual(statuses[-3:], ["dark"] * 3)
        rig.tank.demo("dark")
        rig.run(3)
        self.assertTrue(rig.monitor.lit)
        self.assertEqual(rig.monitor.tracking["stage"], "tracking")

    def test_without_calibration_counts_fish_but_no_speed(self):
        rig = self.rig(Settings(fish_count=2))
        rig.run(5)
        rows = rig.rows("SELECT * FROM seconds")
        self.assertTrue(all(row["status"] == "uncalibrated" and row["speed"] is None for row in rows))
        self.assertTrue(any(row["fish"] == 2 for row in rows))
        self.assertNotIn("x_cm", rig.monitor.tracking["tracks"][0])

    def test_low_activity_raises_and_clears_alert(self):
        rig = self.rig()
        seed_history(rig.store, rig.clock.ms // 1000, 2)
        rig.interval = 500
        rig.run(4 * 60)
        self.assertNotIn("low_activity", rig.monitor.alerts.active)
        rig.tank.demo("sluggish")
        rig.run(6 * 60)
        alert = rig.monitor.alerts.active.get("low_activity")
        self.assertIsNotNone(alert)
        self.assertIsNotNone(alert["snapshot"])
        self.assertTrue((self.directory / "snapshots" / alert["snapshot"]).is_file())
        rig.tank.demo("sluggish")
        rig.run(6 * 60)
        self.assertNotIn("low_activity", rig.monitor.alerts.active)
        kinds = [event["kind"] for event in rig.store.events(0, rig.clock.ms)]
        self.assertEqual(kinds[:2], ["alert_cleared", "alert"])

    def test_camera_offline_alert_after_timeout(self):
        rig = self.rig()
        rig.run(2)
        monitor = rig.monitor
        rig.clock.ms += 61_000
        minutes_before = len(rig.rows("SELECT * FROM minutes"))
        monitor.watchdog_tick()
        self.assertEqual(monitor.camera_status(), "offline")
        self.assertIn("camera_offline", monitor.alerts.active)
        self.assertEqual(len(rig.rows("SELECT * FROM minutes")), minutes_before + 1, "idle flushes the unfinished minute")
        rig.run(1)
        self.assertNotIn("camera_offline", monitor.alerts.active)

    def test_wall_clock_jump_is_not_a_camera_outage(self):
        rig = self.rig()
        monotonic = rig.clock.ms / 1000
        rig.monitor.monotonic = lambda: monotonic
        rig.run(2)
        monotonic += 0.5
        rig.clock.ms += 130 * 86_400_000
        rig.monitor.clock_changed()
        rig.monitor.watchdog_tick()
        self.assertEqual(rig.monitor.alerts.active, {})
        self.assertEqual(rig.monitor.camera_status(), "live")

    def test_feeding_response_compares_before_and_after(self):
        rig = self.rig()
        rig.interval = 200
        rig.run(60)

        asyncio.run(rig.monitor.feed())
        with self.assertRaises(ValueError):
            asyncio.run(rig.monitor.feed())
        rig.run(30)
        response = rig.monitor.feeding_response()
        self.assertTrue(response["collecting"])
        self.assertIsNotNone(response["snapshot"])
        self.assertGreater(response["after"]["height"], response["before"]["height"] + 5,
                           "fish rise to the surface after feeding")
        self.assertGreater(response["before"]["lit_s"], 55)


def write_test_video(path: Path, seconds=3, fps=10):
    """A short goldfish clip rendered by the fake tank."""
    clock = Clock(0)
    tank = FakeTank(clock=lambda: clock.ms / 1000)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (640, 360))
    for _ in range(seconds * fps):
        clock.ms += 1000 // fps
        writer.write(decode(tank.next_frame()))
    writer.release()


class VideoSourceTests(unittest.TestCase):
    def test_plays_in_real_time_and_marks_the_loop(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "clip.avi"
        write_test_video(path, seconds=1)
        source = VideoFile(path, max_width=320)

        async def collect():
            items = []
            async with source.frames() as frames:
                async for item in frames:
                    items.append(item)
                    if len(items) == 13:
                        return items

        items = asyncio.run(collect())
        self.assertEqual(items[10], None, "loop marker after the 10 frames of a 1 s clip")
        self.assertEqual(decode(items[0]).shape, (180, 320, 3))
        self.assertEqual(source.name, "video:clip")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.videos = self.directory / "videos"

    def client(self, start_monitor=False, fake=True, preferred="fake"):
        catalog = Catalog(self.videos, camera=False, fake=fake)
        app = create_app(catalog=catalog, data_root=self.directory, preferred=preferred, start_monitor=start_monitor)
        return self.enterContext(TestClient(app))

    def test_live_websocket_streams_state_then_frame(self):
        client = self.client(start_monitor=True)
        with client.websocket_connect("/ws/live") as socket:
            for _ in range(20):
                state = socket.receive_json()
                if state["camera"]["status"] == "live":
                    break
            self.assertEqual(state["source"], "fake")
            self.assertEqual(state["tracking"]["expected"], 2)
            frame = socket.receive_bytes()
            self.assertTrue(frame.startswith(bytes((0xFF, 0xD8))))
        self.assertEqual(client.get("/api/frame.jpg").headers["content-type"], "image/jpeg")

    def test_feed_settings_history_and_demo(self):
        client = self.client()
        self.assertIsNone(client.get("/api/feeding").json())
        response = client.post("/api/feed")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["collecting"])
        self.assertEqual(client.post("/api/feed").status_code, 409)
        self.assertEqual(client.post("/api/feed", headers={"Origin": "http://evil.example"}).status_code, 403)

        settings = client.get("/api/settings").json()
        bad = {**settings, "calibration": {**settings["calibration"], "corners": [[0, 0], [0, 9], [9, 9], [9, 0]]}}
        self.assertEqual(client.put("/api/settings", json=bad).status_code, 422)
        moved = {**settings, "calibration": {**settings["calibration"], "tank_width_cm": 45}}
        self.assertEqual(client.put("/api/settings", json=moved).status_code, 200)
        self.assertEqual(client.get("/api/state").json()["calibration"]["tank_width_cm"], 45)
        saved = self.directory / "sources" / "fake" / "settings.json"
        self.assertEqual(Settings.model_validate_json(saved.read_text()).calibration.tank_width_cm, 45)

        history = client.get("/api/history?range=1h").json()
        self.assertEqual(history["step_ms"], 60_000)
        self.assertEqual(len(history["heat"]["counts"]), 72)
        self.assertEqual({event["kind"] for event in history["events"]}, {"feed", "calibration"})
        self.assertEqual(client.get("/api/history?range=2h").status_code, 422)

        state = client.post("/api/demo", json={"action": "sluggish"}).json()
        self.assertTrue(state["demo"]["sluggish"])

    def test_upload_video_switch_sources_and_remember_choice(self):
        client = self.client(start_monitor=True)
        path = self.directory / "clip.avi"
        write_test_video(path)
        self.assertEqual(client.put("/api/videos/..%2Fevil.avi", content=b"x").status_code, 404)
        self.assertEqual(client.put("/api/videos/clip.exe", content=b"x").status_code, 422)
        sources = client.put("/api/videos/tank test.avi", content=path.read_bytes()).json()
        self.assertEqual([item["id"] for item in sources], ["video:tank test", "fake"])
        self.assertEqual(client.put("/api/source", json={"id": "picamera"}).status_code, 404)

        state = client.put("/api/source", json={"id": "video:tank test"}).json()
        self.assertEqual(state["source"], "video:tank test")
        self.assertIsNone(state["demo"])
        self.assertEqual(client.post("/api/demo", json={"action": "dark"}).status_code, 409)
        with client.websocket_connect("/ws/live") as socket:
            for _ in range(20):
                state = socket.receive_json()
                if state["camera"]["status"] == "live" and state["tracking"]["fish"]:
                    break
            self.assertEqual(state["source"], "video:tank test")
            self.assertEqual(state["camera"]["width"], 640)
            self.assertEqual(len(state["tracking"]["fish"]), 1, "video defaults to one fish")
        self.assertEqual((self.directory / "active-source.txt").read_text(), "video:tank test")
        self.assertTrue((self.directory / "sources" / "video-tank-test" / "aquarium.sqlite3").is_file())
        self.assertEqual(client.get("/api/snapshots/..%2Fsettings.json").status_code, 404)

    def test_clock_sync_only_when_clocks_disagree(self):
        client = self.client()
        now = client.get("/api/state").json()["now_ms"]
        self.assertEqual(client.post("/api/clock", json={"now_ms": now + 5000}).json()["adjusted"], False)
        with patch("aquarium.main.set_system_clock", return_value=None) as setter:
            result = client.post("/api/clock", json={"now_ms": now + 86_400_000}).json()
        self.assertTrue(result["adjusted"])
        setter.assert_awaited_once()
        self.assertEqual(client.get("/api/events").json()[0]["kind"], "clock")

    def test_falls_back_when_preferred_camera_is_missing(self):
        client = self.client(fake=True, preferred="picamera")
        self.assertEqual(client.get("/api/state").json()["source"], "fake")
        self.assertEqual(client.get("/api/sources").json()[0]["active"], True)


if __name__ == "__main__":
    unittest.main()
