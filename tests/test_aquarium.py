import asyncio
import subprocess
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aquarium.contracts import DeviceUnavailable, FeedCommand, LightCommand
from aquarium.main import create_app


class TestActuator:
    def __init__(self):
        self.commands = []
        self.available = True

    async def execute(self, command):
        if not self.available:
            raise DeviceUnavailable()
        self.commands.append(command)


class AquariumTests(unittest.TestCase):
    def setUp(self):
        self.database = Path(self.enterContext(tempfile.TemporaryDirectory())) / "aquarium.sqlite3"

    def test_simulation_websocket_controls_and_history(self):
        with TestClient(create_app(database=self.database)) as client, client.websocket_connect("/ws/live") as socket:
            state = socket.receive_json()
            if not state["tracking_fresh"]:
                state = socket.receive_json()
            self.assertEqual(state["source"], "simulation")
            self.assertTrue(state["tracking_fresh"])
            self.assertEqual(len(state["fish"]), 8)
            self.assertEqual(set(state["fish"][0]["position"]), {"x", "y"})
            self.assertEqual(len(state["tank"]["feed_point"]), 2)
            self.assertEqual(client.post("/api/control/feed").status_code, 200)
            self.assertEqual(client.post("/api/control/feed").status_code, 409)
            response = client.put("/api/control/light", json={"mode": "off"})
            self.assertFalse(response.json()["controls"]["light_on"])
            history = client.get("/api/history").json()
            self.assertTrue(history["series"])
            self.assertTrue(history["feed"]["collecting"])
            self.assertEqual({event["action"] for event in history["events"]}, {"feed", "light"})

            reset = client.post("/api/demo", json={"action": "reset"}).json()
            self.assertNotEqual(reset["run_id"], state["run_id"])
            history = client.get("/api/history").json()
            self.assertIsNone(history["feed"])
            self.assertEqual(len(history["events"]), 2)

    def test_hardware_ingest_loss_expiry_and_persistence(self):
        app = create_app(mode="hardware", ingest_token="test-token", database=self.database)
        headers = {"Authorization": "Bearer test-token"}
        position = {"x": 5, "y": 10}
        track = {"id": "fish-1", "status": "tracked", "position": position}
        with TestClient(app) as client, patch("aquarium.main.now_ms", return_value=1_800_000_000_000) as clock:
            frame = {"timestamp_ms": clock.return_value, "tracks": [track]}
            telemetry = {"timestamp_ms": clock.return_value, "temperature_c": 25.5, "transmittance_pct": 54}
            observations = "/api/ingest/observations"
            self.assertEqual(client.post(observations, json=frame).status_code, 401)
            legacy = {**frame, "tracks": [{**track, "position": {**position, "z": 0}}]}
            self.assertEqual(client.post(observations, json=legacy, headers=headers).status_code, 422)
            self.assertEqual(client.post(observations, json=frame, headers=headers).status_code, 204)
            self.assertEqual(client.post(observations, json=frame, headers=headers).status_code, 409)
            self.assertEqual(client.post("/api/ingest/telemetry", json=telemetry, headers=headers).status_code, 204)
            self.assertTrue(client.get("/api/state").json()["clarity_alert"])

            clock.return_value += 1000
            frame = {"timestamp_ms": clock.return_value, "tracks": [
                {**track, "status": "lost", "position": {**position, "x": 27}},
            ]}
            self.assertEqual(client.post(observations, json=frame, headers=headers).status_code, 204)
            fish = client.get("/api/state").json()["fish"][0]
            self.assertEqual(fish["status"], "lost")
            self.assertEqual(fish["position"], position)
            self.assertIsNone(fish["velocity"])

            clock.return_value += 1000
            frame = {"timestamp_ms": clock.return_value, "tracks": [track]}
            self.assertEqual(client.post(observations, json=frame, headers=headers).status_code, 204)
            self.assertIsNone(client.get("/api/state").json()["fish"][0]["velocity"])

            clock.return_value += 1000
            frame = {"timestamp_ms": clock.return_value, "tracks": [
                {**track, "position": {"x": 8, "y": 14}},
            ]}
            self.assertEqual(client.post(observations, json=frame, headers=headers).status_code, 204)
            self.assertEqual(client.get("/api/state").json()["fish"][0]["velocity"], [3.0, 4.0])

            clock.return_value += 11_000
            self.assertEqual(client.post(observations, json=frame, headers=headers).status_code, 422)
            state = client.get("/api/state").json()
            self.assertFalse(state["tracking_fresh"])
            self.assertIsNone(state["telemetry"])
            self.assertFalse(state["clarity_alert"])
            self.assertEqual(client.post("/api/control/feed").status_code, 503)

        with TestClient(app) as client, patch("aquarium.main.now_ms", return_value=1_800_000_015_000):
            history = client.get("/api/history?range=24h").json()
            self.assertEqual(history["summary"]["middle_count"], 3)
            self.assertEqual(history["summary"]["speed_cm_s"], 5.0)
            self.assertEqual(history["series"][0]["temperature_c"], 25.5)

    def test_replay_media_and_seek(self):
        directory = self.database.parent
        with TestClient(create_app(database=self.database, replay_directory=directory)) as client:
            video = "/api/replay/video"
            tracks = "/api/replay/tracks"
            self.assertEqual(client.get(video).status_code, 404)
            self.assertEqual(client.get(tracks).status_code, 404)
            (directory / "front.webm").write_bytes(b"0123456789")
            (directory / "tracks.csv").write_text("frame,time_s,id,status,x_px,y_px\n1,0,1,tracked,12,24\n")
            response = client.get(video, headers={"Range": "bytes=2-5"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
            self.assertEqual(response.content, b"2345")
            self.assertIn("1,0,1,tracked,12,24", client.get(tracks).text)

    def test_hardware_controls_require_device_confirmation(self):
        actuator = TestActuator()
        app = create_app(mode="hardware", ingest_token="test-token", database=self.database, actuator=actuator)
        with TestClient(app) as client:
            actuator.available = False
            self.assertEqual(client.post("/api/control/feed").status_code, 503)
            self.assertIsNone(client.get("/api/state").json()["controls"]["last_feed_ms"])
            self.assertEqual(client.get("/api/history").json()["events"], [])

            actuator.available = True
            self.assertEqual(client.post("/api/control/feed").status_code, 200)
            self.assertEqual(client.post("/api/control/feed").status_code, 409)
            self.assertEqual(client.put("/api/control/light", json={"mode": "on"}).status_code, 200)
            self.assertEqual(client.put("/api/control/light", json={"mode": "on"}).status_code, 200)
            self.assertEqual([type(command) for command in actuator.commands], [FeedCommand, LightCommand])

    def test_camera_can_be_disabled(self):
        with TestClient(create_app(database=self.database)) as client:
            self.assertFalse(client.get("/api/state").json()["camera_enabled"])
            self.assertEqual(client.post("/api/camera/snapshot").status_code, 503)
            with client.websocket_connect("/ws/camera/live") as socket:
                self.assertEqual(socket.receive_text(), "相機預覽尚未啟用")

    def test_simulation_switch_clears_live_data_and_switches_history(self):
        app = create_app(database=self.database, ingest_token="test-token")
        with TestClient(app) as client, client.websocket_connect("/ws/live") as socket:
            simulated = socket.receive_json()
            self.assertEqual(simulated["source"], "simulation")
            self.assertTrue(client.get("/api/history").json()["series"])
            self.assertEqual(client.put("/api/simulation", json={"enabled": False},
                                        headers={"Origin": "https://example.com"}).status_code, 403)
            switched = client.put("/api/simulation", json={"enabled": False}).json()
            self.assertEqual(switched["source"], "hardware")
            self.assertNotEqual(switched["run_id"], simulated["run_id"])
            self.assertEqual(switched["fish"], [])
            self.assertIsNone(switched["telemetry"])
            self.assertIsNone(switched["controls"]["next_feed_ms"])
            self.assertIsNone(switched["controls"]["light_on"])
            self.assertEqual(client.post("/api/demo", json={"action": "lost"}).status_code, 409)
            self.assertEqual(client.post("/api/control/feed").status_code, 503)
            history = client.get("/api/history").json()
            self.assertEqual(history["source"], "hardware")
            self.assertEqual(history["series"], [])

            telemetry = {"timestamp_ms": switched["timestamp_ms"], "temperature_c": 24}
            headers = {"Authorization": "Bearer test-token"}
            self.assertEqual(client.post("/api/ingest/telemetry", json=telemetry, headers=headers).status_code, 204)
            self.assertEqual(client.get("/api/state").json()["telemetry"]["temperature_c"], 24)
            resumed = client.put("/api/simulation", json={"enabled": True}).json()
            self.assertEqual(resumed["source"], "simulation")
            self.assertEqual(client.post("/api/ingest/telemetry", json=telemetry, headers=headers).status_code, 409)
            self.assertEqual(client.get("/api/history").json()["source"], "simulation")

    def test_camera_snapshot_and_timeout(self):
        jpeg = b"\xff\xd8image\xff\xd9"
        with TestClient(create_app(database=self.database, camera_enabled=True)) as client:
            self.assertTrue(client.get("/api/state").json()["camera_enabled"])
            snapshot = "/api/camera/snapshot"
            with patch("aquarium.camera.subprocess.run") as capture:
                capture.return_value = subprocess.CompletedProcess([], 0, jpeg, b"")
                self.assertEqual(client.post(snapshot, headers={"Origin": "https://example.com"}).status_code, 403)
                capture.assert_not_called()
                response = client.post(snapshot, headers={"Origin": "http://testserver"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/jpeg")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.content, jpeg)

                capture.side_effect = [subprocess.TimeoutExpired("rpicam-still", 10), capture.return_value]
                with self.assertLogs("aquarium.camera", level="WARNING"):
                    self.assertEqual(client.post(snapshot).status_code, 503)
                self.assertEqual(client.post(snapshot).status_code, 200)

    def test_camera_live_delivers_frames_and_releases_camera_on_disconnect(self):
        first = b"\xff\xd8first\xff\xd9"
        second = b"\xff\xd8second\xff\xd9"

        class CameraProcess:
            returncode = None

            def __init__(self):
                self.stdout = self
                self.chunks = iter((first[:1], first[1:] + second[:-1], second[-1:]))

            async def read(self, _size):
                chunk = next(self.chunks, None)
                if chunk is None:
                    await asyncio.Event().wait()
                return chunk

            def terminate(self):
                self.returncode = -15

            async def communicate(self):
                return b"", None

        process = CameraProcess()
        spawn = AsyncMock(return_value=process)
        path = "/ws/camera/live"
        with (TestClient(create_app(database=self.database, camera_enabled=True)) as client,
              patch("aquarium.camera.asyncio.create_subprocess_exec", new=spawn),
              patch("aquarium.camera.subprocess.run") as capture):
            capture.return_value = subprocess.CompletedProcess([], 0, first, b"")
            with self.assertRaises(WebSocketDisconnect), client.websocket_connect(
                path, headers={"Origin": "https://example.com"}
            ):
                pass
            spawn.assert_not_awaited()
            with client.websocket_connect(path, headers={"Origin": "http://testserver"}) as socket:
                self.assertEqual(socket.receive_bytes(), first)
                self.assertEqual(socket.receive_bytes(), second)
                state = client.get("/api/state").json()
                self.assertEqual(state["source"], "hardware")
                self.assertTrue(state["camera_live"])
                self.assertEqual(state["fish"], [])
                self.assertIsNone(state["telemetry"])
                self.assertEqual(client.put("/api/simulation", json={"enabled": True}).status_code, 409)
                self.assertEqual(client.post("/api/camera/snapshot").status_code, 503)
                with client.websocket_connect(path) as other:
                    self.assertIn("相機使用中", other.receive_text())
                capture.assert_not_called()
            self.assertEqual(process.returncode, -15)
            self.assertEqual(client.post("/api/camera/snapshot").content, first)
            stopped = client.get("/api/state").json()
            self.assertFalse(stopped["camera_live"])
            self.assertEqual(stopped["source"], "hardware")
            self.assertIsNone(stopped["observation_timestamp_ms"])

    def test_live_camera_tracks_jpeg_pixels_and_drops_missing_detections(self):
        try:
            import cv2
            import numpy as np
            from aquarium.vision import LiveFishTracker
        except ImportError:
            self.skipTest("OpenCV and NumPy are required for live tracking")

        def jpeg(x=None):
            image = np.full((240, 320), 215, dtype=np.uint8)
            if x is not None:
                cv2.ellipse(image, (x, 100), (12, 4), 0, 0, 360, 35, -1)
            return cv2.imencode(".jpg", image)[1].tobytes()

        empty, first, moved = jpeg(), jpeg(120), jpeg(128)
        large_gold = np.full((240, 320, 3), 215, dtype=np.uint8)
        cv2.ellipse(large_gold, (220, 110), (42, 22), 0, 0, 360, (12, 141, 232), -1)
        cv2.rectangle(large_gold, (0, 0), (13, 13), (12, 141, 232), -1)
        gold_jpeg = cv2.imencode(".jpg", large_gold)[1].tobytes()
        gold = LiveFishTracker(2, "gold").process(gold_jpeg)
        self.assertEqual(len(gold["tracks"]), 1)
        self.assertAlmostEqual(gold["tracks"][0]["x_px"], 220, delta=1)

        @asynccontextmanager
        async def live():
            async def frames():
                for _ in range(60):
                    yield empty
                yield first
                yield moved
                yield empty
                await asyncio.Event().wait()
            yield frames()

        with patch("aquarium.main.RaspberryPiCamera") as camera:
            camera.return_value.live = live
            app = create_app(database=self.database, camera_enabled=True)
            with TestClient(app) as client, client.websocket_connect("/ws/camera/live?fish=1") as socket:
                for index in range(60):
                    status = socket.receive_json()
                    self.assertEqual(socket.receive_bytes(), empty)
                    self.assertEqual(status["stage"], "learning" if index < 59 else "tracking")
                    self.assertEqual(status["tracks"], [])
                initial = socket.receive_json()["tracks"][0]
                self.assertEqual(socket.receive_bytes(), first)
                self.assertAlmostEqual(initial["x_px"], 120, delta=1)
                motion = socket.receive_json()["tracks"][0]
                self.assertEqual(socket.receive_bytes(), moved)
                self.assertEqual(initial["id"], motion["id"])
                self.assertEqual(len(motion["trail"]), 2)
                self.assertAlmostEqual(motion["x_px"], 128, delta=1)
                self.assertEqual(socket.receive_json()["tracks"], [])
                self.assertEqual(socket.receive_bytes(), empty)
                state = client.get("/api/state").json()
                self.assertEqual(state["source"], "hardware")
                self.assertEqual(state["camera_tracking"]["tracks"], [])
                self.assertIsNone(state["telemetry"])
