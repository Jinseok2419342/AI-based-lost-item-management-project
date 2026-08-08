"""물품 수명주기 서비스.

감지기 콜백 → 즉시 등록('분석 중...') → 백그라운드 AI 식별 → 분류/기한 확정.
AI가 사람/그림자 등('ignore')이라 판단하면 등록을 철회한다.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import cv2
import numpy as np

from ..ai.classifier import classify_image
from ..config import PATCH_DIR, PHOTO_DIR, config
from ..database import Database

log = logging.getLogger("items")

CATEGORY_KO = {"valuable": "귀중품", "general": "일반 물품", "food": "음식"}


def _now() -> datetime:
    return datetime.now()


def _deadline_for(category: str, base: datetime | None = None) -> str:
    base = base or _now()
    return (base + timedelta(days=config.days_for_category(category))).isoformat(
        timespec="seconds"
    )


class ItemService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.detector = None  # main에서 주입 (순환 참조 방지)
        self._ai_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai")
        self._lock = threading.Lock()

    def attach_detector(self, detector) -> None:
        self.detector = detector
        # 서버 재시작 시 보관 중 물품의 트랙 복원 (같은 카메라 구도 가정)
        for item in self.db.stored_items():
            if not item["bbox"] or not item["patch_path"]:
                continue
            patch = cv2.imread(item["patch_path"], cv2.IMREAD_GRAYSCALE)
            if patch is None:
                continue
            detector.add_track(item["id"], item["bbox"], patch)

    # ── 감지기 콜백 ───────────────────────────────────────────
    def register_from_camera(self, crop_bgr: np.ndarray, bbox, patch: np.ndarray) -> int | None:
        """새 물체 감지 → 즉시 등록하고 AI 식별은 비동기로."""
        try:
            item_id = self.db.insert_item(
                name="분석 중...",
                category="general",
                deadline=_deadline_for("general"),
                bbox=json.dumps(list(bbox)),
                source="camera",
            )
            photo_path = str(PHOTO_DIR / f"item_{item_id}.jpg")
            patch_path = str(PATCH_DIR / f"patch_{item_id}.png")
            cv2.imwrite(photo_path, crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            cv2.imwrite(patch_path, patch)
            self.db.update_item(item_id, photo_path=photo_path, patch_path=patch_path)
            self.db.add_event(
                "item_registered", f"새 물건이 감지되어 #{item_id}로 등록되었습니다.", item_id
            )
            ok, jpeg = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if ok:
                self._ai_pool.submit(self._classify_async, item_id, jpeg.tobytes())
            else:
                self.db.update_item(item_id, ai_status="failed", name="미확인 물품")
            return item_id
        except Exception:  # noqa: BLE001 — 감지 스레드로 예외 전파 금지
            log.exception("물품 등록 실패")
            return None

    def mark_removed_by_camera(self, item_id: int) -> None:
        item = self.db.get_item(item_id)
        if not item or item["status"] != "stored":
            return
        self.db.update_item(
            item_id, status="retrieved", retrieved_at=_now().isoformat(timespec="seconds")
        )
        self.db.add_event(
            "item_retrieved",
            f"'{item['name']}' (#{item_id}) 이(가) 사라진 것을 감지해 회수 처리했습니다.",
            item_id,
        )

    def log_event(self, type_: str, message: str) -> None:
        # 매 움직임 이벤트는 로그가 너무 많아지므로 motion은 저장하지 않음
        if type_ == "motion":
            return
        self.db.add_event(type_, message)

    # ── AI 식별 ──────────────────────────────────────────────
    def _classify_async(self, item_id: int, jpeg: bytes) -> None:
        try:
            result = classify_image(jpeg)
            item = self.db.get_item(item_id)
            if not item:
                return
            if result["category"] == "ignore":
                # 사람·그림자 등 물건이 아님 → 등록 철회
                self.db.delete_item(item_id)
                if self.detector:
                    self.detector.remove_track(item_id)
                self.db.add_event(
                    "item_ignored",
                    f"감지된 영역이 물건이 아니라고 판단되어 등록을 취소했습니다. ({result['name']})",
                )
                return
            registered_at = datetime.fromisoformat(item["registered_at"])
            self.db.update_item(
                item_id,
                name=result["name"],
                category=result["category"],
                description=result["description"],
                deadline=_deadline_for(result["category"], registered_at),
                ai_provider=result["provider"],
                ai_confidence=result["confidence"],
                ai_status="done" if result.get("ok") else "failed",
            )
            self.db.add_event(
                "ai_classified",
                f"#{item_id} 물품을 '{result['name']}'({CATEGORY_KO.get(result['category'], result['category'])})으로 식별했습니다. "
                f"[{result['provider']}, 확신도 {result['confidence']:.0%}]",
                item_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("AI 식별 처리 실패 (#%s)", item_id)
            self.db.update_item(item_id, ai_status="failed", name="미확인 물품")

    def reclassify(self, item_id: int) -> bool:
        """저장된 사진으로 AI 재식별 요청."""
        item = self.db.get_item(item_id)
        if not item or not item["photo_path"]:
            return False
        img = cv2.imread(item["photo_path"])
        if img is None:
            return False
        ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            return False
        self.db.update_item(item_id, ai_status="pending", name="분석 중...")
        self._ai_pool.submit(self._classify_async, item_id, jpeg.tobytes())
        return True

    # ── 수동 조작 (웹 UI) ─────────────────────────────────────
    def set_status(self, item_id: int, status: str) -> bool:
        item = self.db.get_item(item_id)
        if not item or status not in ("stored", "retrieved", "disposed"):
            return False
        fields: dict = {"status": status}
        now = _now().isoformat(timespec="seconds")
        if status == "retrieved":
            fields["retrieved_at"] = now
        elif status == "disposed":
            fields["disposed_at"] = now
        self.db.update_item(item_id, **fields)
        if status in ("retrieved", "disposed") and self.detector:
            self.detector.remove_track(item_id)
        label = {"stored": "보관 중", "retrieved": "회수됨", "disposed": "폐기됨"}[status]
        self.db.add_event(
            "status_changed", f"'{item['name']}' (#{item_id}) 상태를 '{label}'(으)로 변경했습니다.", item_id
        )
        return True

    def set_category(self, item_id: int, category: str) -> bool:
        item = self.db.get_item(item_id)
        if not item or category not in ("valuable", "general", "food"):
            return False
        registered_at = datetime.fromisoformat(item["registered_at"])
        self.db.update_item(
            item_id,
            category=category,
            deadline=_deadline_for(category, registered_at),
            warn_sent=0,
            expire_sent=0,
        )
        self.db.add_event(
            "category_changed",
            f"'{item['name']}' (#{item_id}) 분류를 '{CATEGORY_KO[category]}'(으)로 변경했습니다 (기한 재계산).",
            item_id,
        )
        return True

    def register_manual(self, jpeg: bytes) -> int:
        """관리자가 사진 업로드로 직접 등록 (카메라 없이도 시연 가능)."""
        arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            raise ValueError("이미지를 해석할 수 없습니다.")
        item_id = self.db.insert_item(
            name="분석 중...",
            category="general",
            deadline=_deadline_for("general"),
            source="manual",
        )
        photo_path = str(PHOTO_DIR / f"item_{item_id}.jpg")
        cv2.imwrite(photo_path, arr, [cv2.IMWRITE_JPEG_QUALITY, 92])
        self.db.update_item(item_id, photo_path=photo_path)
        self.db.add_event("item_registered", f"관리자가 #{item_id} 물품을 직접 등록했습니다.", item_id)
        ok, enc = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:
            self._ai_pool.submit(self._classify_async, item_id, enc.tobytes())
        return item_id
