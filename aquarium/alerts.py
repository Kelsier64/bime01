"""Alert rules. Active alerts live in memory; raises and clears are logged as events."""

from collections.abc import Callable

from .settings import Settings
from .store import Store

MIN_LIT_SECONDS = 30
MIN_BASELINE_MINUTES = 180


class Alerts:
    def __init__(self, store: Store, snapshot: Callable[[str], str | None]):
        self.store = store
        self.snapshot = snapshot
        self.active: dict[str, dict] = {}

    def raise_alert(self, kind, message, ts_ms, detail=None, snapshot=True):
        if kind in self.active:
            self.active[kind]["message"] = message
            return
        image = self.snapshot(kind) if snapshot else None
        self.active[kind] = {"kind": kind, "message": message, "since_ms": ts_ms, "snapshot": image}
        self.store.add_event(ts_ms, "alert", {"alert": kind, "message": message, **(detail or {})}, image)

    def clear(self, kind, ts_ms):
        alert = self.active.pop(kind, None)
        if alert is not None:
            self.store.add_event(ts_ms, "alert_cleared", {
                "alert": kind, "duration_s": round((ts_ms - alert["since_ms"]) / 1000),
            })

    def check_camera(self, now_ms, last_frame_ms, started_ms, timeout_s):
        reference = last_frame_ms if last_frame_ms is not None else started_ms
        if now_ms - reference > timeout_s * 1000:
            self.raise_alert("camera_offline", f"相機超過 {timeout_s} 秒沒有送出畫面", now_ms, snapshot=False)

    def check_activity(self, now_s: int, settings: Settings):
        window = settings.low_activity_window_min * 60
        recent, recent_minutes = self.store.activity(now_s - window, now_s, MIN_LIT_SECONDS)
        if recent is None or recent_minutes < max(3, settings.low_activity_window_min // 2):
            return
        baseline, baseline_minutes = self.store.activity(
            now_s - settings.baseline_days * 86_400, now_s - window, MIN_LIT_SECONDS,
        )
        if not baseline or baseline_minutes < MIN_BASELINE_MINUTES:
            return
        ratio = recent / baseline
        if ratio < settings.low_activity_ratio:
            message = (f"最近 {settings.low_activity_window_min} 分鐘平均游速 {recent:.1f} cm/s，"
                       f"只有過去 {settings.baseline_days} 天平均 {baseline:.1f} cm/s 的 {ratio:.0%}")
            self.raise_alert("low_activity", message, now_s * 1000, {
                "recent_cm_s": round(recent, 2), "baseline_cm_s": round(baseline, 2),
            })
        elif ratio >= min(0.95, settings.low_activity_ratio + 0.15):
            self.clear("low_activity", now_s * 1000)
