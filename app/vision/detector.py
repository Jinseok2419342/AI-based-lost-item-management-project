"""장면 감시 및 물체 등장/회수 감지.

동작 원리(상태 머신):
  IDLE(안정) ── 움직임 발생 ──▶ MOTION ── 움직임 멈춤 ──▶ SETTLING
  SETTLING 에서 settle_seconds 동안 계속 안정되면 → 기준 장면과 현재 장면을 비교(_analyze)
  분석 후 현재 장면이 새 기준(reference)이 된다.

비교(_analyze)에서:
  1. 등록된 물체(트랙)의 자리가 변했으면 템플릿 매칭으로 아직 있는지 확인 → 없으면 '회수됨'
  2. 트랙과 무관한 변화 영역 중 '무언가 생긴' 영역(엣지 밀도 증가)을 새 물체로 등록
  3. 화면 대부분이 바뀌었으면(조명 변화·카메라 이동) 등록 없이 기준만 갱신
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from ..config import config
from .camera import Camera

log = logging.getLogger("detector")

BLUR = (21, 21)
DIFF_THRESHOLD = 25          # 픽셀 차이 임계값
REMOVAL_MASK_RATIO = 0.25    # bbox 안 변화 비율이 이 이상이면 존재 재확인
SEARCH_MARGIN = 24           # 템플릿 매칭 탐색 여유(px)
MERGE_GAP = 24               # 이 거리 안의 변화 영역은 하나의 물체로 병합
PROCESS_FPS = 10


@dataclass
class Track:
    """보관 중인 물체의 화면상 위치와 외형(그레이스케일 패치)."""
    item_id: int
    bbox: tuple[int, int, int, int]     # (x, y, w, h)
    patch: np.ndarray                   # 등록 시점의 그레이스케일 크롭
    recheck: bool = False               # 가림 발생 후 존재 재확인 필요

    def as_dict(self) -> dict:
        x, y, w, h = self.bbox
        return {"item_id": self.item_id, "bbox": [x, y, w, h]}


@dataclass
class DetectorCallbacks:
    """감지 이벤트를 서비스 계층으로 전달."""
    on_new_object: object = None      # fn(crop_bgr, bbox, patch_gray) -> int|None (item_id)
    on_object_removed: object = None  # fn(item_id) -> None
    on_event: object = None           # fn(type, message) -> None


class SceneWatcher:
    def __init__(self, camera: Camera, callbacks: DetectorCallbacks) -> None:
        self.camera = camera
        self.cb = callbacks
        self.state = "idle"            # idle | motion | settling | no_camera
        self.last_event_at: float = 0.0
        self._tracks: list[Track] = []
        self._tracks_lock = threading.Lock()
        self._ref: np.ndarray | None = None      # 기준 장면(그레이, 블러) — 변화 마스크용
        self._ref_raw: np.ndarray | None = None  # 기준 장면(그레이, 원본) — 엣지·패치용
        self._prev: np.ndarray | None = None     # 직전 프레임(움직임 계산용)
        self._settle_start = 0.0
        self._motion_start = 0.0
        self._rebaseline_req = threading.Event()
        self._last_seq = -1
        self._frame_size: tuple[int, int] = (0, 0)   # (w, h)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.paused = False

    # ── lifecycle ────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    # ── 외부 제어/조회 ────────────────────────────────────────
    def status(self) -> dict:
        w, h = self._frame_size
        with self._tracks_lock:
            tracks = [t.as_dict() for t in self._tracks]
        settle_remaining = None
        if self.state == "settling":
            settle_seconds = min(max(config.get_float("settle_seconds"), 0.5), 60.0)
            settle_remaining = max(
                0.0, round(settle_seconds - (time.monotonic() - self._settle_start), 1)
            )
        return {
            "state": "no_camera" if not self.camera.connected else self.state,
            "camera_connected": self.camera.connected,
            "paused": self.paused,
            "frame_width": w,
            "frame_height": h,
            "settle_remaining": settle_remaining,
            "tracks": tracks,
        }

    def rebaseline(self) -> None:
        """현재 화면을 새 기준으로 삼도록 요청한다(감지 초기화, 트랙은 유지).

        실제 초기화는 감지 스레드가 다음 스텝에서 수행한다 — 다른 스레드가
        _ref/_prev를 직접 건드리면 분석 도중 끼어들어 등록이 누락될 수 있다.
        """
        self._rebaseline_req.set()

    def add_track(self, item_id: int, bbox, patch: np.ndarray) -> None:
        with self._tracks_lock:
            self._tracks.append(Track(item_id, tuple(bbox), patch))

    def remove_track(self, item_id: int) -> None:
        with self._tracks_lock:
            self._tracks = [t for t in self._tracks if t.item_id != item_id]

    def has_track(self, item_id: int) -> bool:
        with self._tracks_lock:
            return any(t.item_id == item_id for t in self._tracks)

    # ── main loop ────────────────────────────────────────────
    def _run(self) -> None:
        interval = 1.0 / PROCESS_FPS
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._step()
            except Exception:  # noqa: BLE001 — 감지 루프는 절대 죽으면 안 됨
                log.exception("감지 루프 오류")
                time.sleep(1)
            delay = interval - (time.monotonic() - t0)
            if delay > 0:
                time.sleep(delay)

    def _step(self) -> None:
        frame, seq = self.camera.latest()
        if frame is None or seq == self._last_seq:
            return
        self._last_seq = seq
        h, w = frame.shape[:2]
        self._frame_size = (w, h)

        if self._rebaseline_req.is_set():
            self._rebaseline_req.clear()
            self._ref = None
            self._ref_raw = None
            self._prev = None
            self.state = "idle"

        raw = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(raw, BLUR, 0)

        if self._ref is None or self._prev is None or self._ref.shape != gray.shape:
            self._ref = gray.astype(np.float32)
            self._ref_raw = raw
            self._prev = gray
            self.state = "idle"
            return

        # 직전 프레임 대비 움직임 비율
        motion_diff = cv2.absdiff(gray, self._prev)
        motion_ratio = float(
            (motion_diff > DIFF_THRESHOLD).sum() / motion_diff.size
        )
        self._prev = gray

        if self.paused:
            self.state = "idle"
            # 일시정지 중엔 기준을 현재 화면으로 계속 갱신해 재개 시 오탐 방지
            self._ref = gray.astype(np.float32)
            self._ref_raw = raw
            return

        # 설정 이상값(0·음수 등)으로 상태머신이 교착하지 않도록 안전 범위로 클램프
        motion_threshold = min(max(config.get_float("motion_threshold"), 0.0005), 0.5)
        stable_threshold = motion_threshold / 3.0
        settle_seconds = min(max(config.get_float("settle_seconds"), 0.5), 60.0)

        if self.state == "idle":
            if motion_ratio > motion_threshold:
                self.state = "motion"
                self._motion_start = time.monotonic()
                self._emit("motion", "움직임이 감지되었습니다.")
            else:
                # 서서히 변하는 조명을 흡수 (정지 상태에서만, 아주 천천히)
                cv2.accumulateWeighted(gray, self._ref, 0.005)
        elif self.state == "motion":
            if motion_ratio < stable_threshold:
                self.state = "settling"
                self._settle_start = time.monotonic()
            elif time.monotonic() - self._motion_start > 90.0:
                # 선풍기·화면 깜빡임 등 끝나지 않는 미세 움직임 → 등록 없이 기준만 갱신
                self._ref = gray.astype(np.float32)
                self._ref_raw = raw
                self.state = "idle"
                self._emit(
                    "rebaseline",
                    "움직임이 90초 이상 계속되어 물건 등록 없이 기준 화면을 갱신했습니다. "
                    "(민감도 설정을 확인하세요)",
                )
        elif self.state == "settling":
            if motion_ratio > stable_threshold:
                self.state = "motion"
            elif time.monotonic() - self._settle_start >= settle_seconds:
                self._analyze(self._ref.astype(np.uint8), gray, self._ref_raw, raw, frame)
                self._ref = gray.astype(np.float32)
                self._ref_raw = raw
                self.state = "idle"

    # ── scene comparison ─────────────────────────────────────
    def _analyze(
        self,
        ref: np.ndarray,
        cur: np.ndarray,
        ref_raw: np.ndarray,
        cur_raw: np.ndarray,
        frame_bgr: np.ndarray,
    ) -> None:
        self.last_event_at = time.time()
        h, w = cur.shape
        diff = cv2.absdiff(ref, cur)
        _, mask = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.dilate(mask, kernel, iterations=2)

        change_ratio = float(mask.sum()) / (255.0 * mask.size)
        if change_ratio > config.get_float("max_change_ratio"):
            self._emit(
                "rebaseline",
                f"화면의 {change_ratio:.0%}가 변해 조명 변화/카메라 이동으로 판단, 기준 화면을 갱신했습니다.",
            )
            return

        with self._tracks_lock:
            tracks = list(self._tracks)
        # 변화가 전혀 없어도, 가림 후 재확인이 필요한 트랙이 있으면 검사는 진행한다
        if change_ratio == 0 and not any(t.recheck for t in tracks):
            return

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        all_change_boxes = [cv2.boundingRect(c) for c in contours]

        # 1) 기존 물체가 아직 있는지 확인 → 없어졌으면 회수 처리
        removed_boxes: list[tuple[int, int, int, int]] = []
        for track in tracks:
            x, y, tw, th = self._clamp_box(track.bbox, w, h)
            if tw <= 0 or th <= 0:
                continue
            region = mask[y : y + th, x : x + tw]
            changed = (
                region.size > 0
                and float(region.sum()) / (255.0 * region.size) >= REMOVAL_MASK_RATIO
            )
            if not changed and not track.recheck:
                continue  # 그 자리 변화 없음 → 그대로 있음
            # 물체보다 훨씬 큰 변화 영역이 덮고 있으면 사람/큰 물체에 가려진 것 →
            # 회수로 판단하지 않고, 가림이 걷힌 뒤의 분석에서 존재를 재확인한다
            track_area = max(1, tw * th)
            occluded = any(
                _intersects(b, (x, y, tw, th)) and b[2] * b[3] > 3 * track_area
                for b in all_change_boxes
            )
            if occluded:
                track.recheck = True
                continue
            score = self._match_score(cur_raw, track, w, h)
            track.recheck = False
            if score < config.get_float("match_threshold"):
                removed_boxes.append(track.bbox)
                self.remove_track(track.item_id)
                if self.cb.on_object_removed:
                    self.cb.on_object_removed(track.item_id)

        # 2) 새로 나타난 물체 등록
        min_area = config.get_float("min_area_ratio") * w * h
        boxes = [
            cv2.boundingRect(c) for c in contours if cv2.contourArea(c) >= min_area
        ]
        boxes = _merge_boxes(boxes)

        with self._tracks_lock:
            live_boxes = [t.bbox for t in self._tracks]

        for box in boxes:
            if any(_overlap_ratio(box, rb) > 0.3 for rb in removed_boxes):
                continue  # 회수된 물체가 있던 자리의 변화
            if any(_overlap_ratio(box, lb) > 0.5 for lb in live_boxes):
                continue  # 기존 물체 주변의 그림자/미세 이동
            x, y, bw, bh = self._clamp_box(box, w, h)
            if bw < 12 or bh < 12:
                continue
            # 등장 vs 소멸 구분: 물체가 '생겼다'면 현재 크롭의 엣지가 더 풍부하다
            ref_edges = int((cv2.Canny(ref_raw[y : y + bh, x : x + bw], 60, 160) > 0).sum())
            cur_edges = int((cv2.Canny(cur_raw[y : y + bh, x : x + bw], 60, 160) > 0).sum())
            if cur_edges < ref_edges * 0.8:
                continue  # 등록 안 된 무언가가 치워진 자국
            pad = 16
            px0, py0 = max(0, x - pad), max(0, y - pad)
            px1, py1 = min(w, x + bw + pad), min(h, y + bh + pad)
            crop = frame_bgr[py0:py1, px0:px1].copy()
            patch = cur_raw[y : y + bh, x : x + bw].copy()
            if self.cb.on_new_object:
                item_id = self.cb.on_new_object(crop, (x, y, bw, bh), patch)
                if item_id is not None:
                    self.add_track(item_id, (x, y, bw, bh), patch)

    def _match_score(self, cur: np.ndarray, track: Track, w: int, h: int) -> float:
        """트랙 위치 주변에서 저장된 패치를 찾는다. 1.0=확실히 있음."""
        x, y, tw, th = track.bbox
        patch = track.patch
        ph, pw = patch.shape[:2]
        x0, y0 = max(0, x - SEARCH_MARGIN), max(0, y - SEARCH_MARGIN)
        x1, y1 = min(w, x + tw + SEARCH_MARGIN), min(h, y + th + SEARCH_MARGIN)
        window = cur[y0:y1, x0:x1]
        if window.shape[0] < ph or window.shape[1] < pw:
            return 0.0
        res = cv2.matchTemplate(window, patch, cv2.TM_CCOEFF_NORMED)
        res = np.nan_to_num(res, nan=0.0)
        return float(res.max())

    @staticmethod
    def _clamp_box(box, w: int, h: int) -> tuple[int, int, int, int]:
        x, y, bw, bh = box
        x, y = max(0, int(x)), max(0, int(y))
        return x, y, min(int(bw), w - x), min(int(bh), h - y)

    def _emit(self, type_: str, message: str) -> None:
        if self.cb.on_event:
            self.cb.on_event(type_, message)


# ── box utils ────────────────────────────────────────────────
def _expand(box, gap: int):
    x, y, w, h = box
    return x - gap, y - gap, w + gap * 2, h + gap * 2


def _intersects(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _union(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = min(ax, bx), min(ay, by)
    x1, y1 = max(ax + aw, bx + bw), max(ay + ah, by + bh)
    return x0, y0, x1 - x0, y1 - y0


def _overlap_ratio(a, b) -> float:
    """교집합 면적 / 작은 박스 면적."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    smaller = max(1, min(aw * ah, bw * bh))
    return inter / smaller


def _merge_boxes(boxes: list) -> list:
    """가까운 변화 영역들을 하나의 물체로 병합."""
    boxes = list(boxes)
    merged = True
    while merged:
        merged = False
        out = []
        while boxes:
            cur = boxes.pop()
            i = 0
            while i < len(boxes):
                if _intersects(_expand(cur, MERGE_GAP), _expand(boxes[i], MERGE_GAP)):
                    cur = _union(cur, boxes.pop(i))
                    merged = True
                else:
                    i += 1
            out.append(cur)
        boxes = out
    return boxes
