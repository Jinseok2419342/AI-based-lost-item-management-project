"""관리자 웹 REST API."""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..ai.classifier import resolve_provider
from ..config import SECRET_KEYS, config
from ..database import Database
from ..services.items import ItemService
from ..services.notifier import send_email
from ..services.scheduler import DeadlineScheduler
from ..vision.camera import Camera
from ..vision.detector import SceneWatcher

log = logging.getLogger("api")


class ItemPatch(BaseModel):
    status: str | None = None
    category: str | None = None
    name: str | None = None
    description: str | None = None
    deadline: str | None = None


class SettingsPut(BaseModel):
    values: dict[str, str]


class PauseBody(BaseModel):
    paused: bool


def _no_camera_jpeg() -> bytes:
    img = np.full((540, 960, 3), 28, np.uint8)
    cv2.putText(img, "NO CAMERA SIGNAL", (300, 260), cv2.FONT_HERSHEY_SIMPLEX,
                1.1, (120, 120, 120), 2, cv2.LINE_AA)
    cv2.putText(img, "check CAMERA_SOURCE in settings", (300, 300),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (90, 90, 90), 1, cv2.LINE_AA)
    _, jpeg = cv2.imencode(".jpg", img)
    return jpeg.tobytes()


def create_router(
    db: Database,
    items: ItemService,
    camera: Camera,
    watcher: SceneWatcher,
    scheduler: DeadlineScheduler,
) -> APIRouter:
    r = APIRouter(prefix="/api")

    # ── items ────────────────────────────────────────────────
    @r.get("/items")
    def list_items(status: str | None = None, category: str | None = None, q: str | None = None):
        return {"items": db.list_items(status=status, category=category, query=q)}

    @r.get("/items/{item_id}")
    def get_item(item_id: int):
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "물품을 찾을 수 없습니다.")
        return item

    @r.patch("/items/{item_id}")
    def patch_item(item_id: int, body: ItemPatch):
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "물품을 찾을 수 없습니다.")
        if body.status is not None and not items.set_status(item_id, body.status):
            raise HTTPException(400, "잘못된 상태 값입니다.")
        if body.category is not None and not items.set_category(item_id, body.category):
            raise HTTPException(400, "잘못된 분류 값입니다.")
        fields: dict = {}
        if body.name is not None and body.name.strip() != item["name"]:
            fields["name"] = body.name.strip()[:80] or "미확인 물품"
        if body.description is not None and body.description.strip() != item["description"]:
            fields["description"] = body.description.strip()[:300]
        if body.deadline is not None:
            d = body.deadline.strip()
            try:
                if len(d) == 10:  # YYYY-MM-DD
                    dt = datetime.fromisoformat(d).replace(hour=23, minute=59, second=59)
                else:
                    dt = datetime.fromisoformat(d)
            except ValueError:
                raise HTTPException(400, "기한 형식이 잘못되었습니다. (YYYY-MM-DD)")
            fields["deadline"] = dt.isoformat(timespec="seconds")
            fields["warn_sent"] = 0
            fields["expire_sent"] = 0
            db.add_event("deadline_changed",
                         f"'{item['name']}' (#{item_id}) 폐기 기한을 {d}(으)로 변경했습니다.", item_id)
        if fields:
            # 관리자가 직접 수정한 아이템에는 늦게 도착한 AI 결과를 덮어쓰지 않는다
            if item["ai_status"] == "pending" and ("name" in fields or "deadline" in fields):
                fields["ai_status"] = "done"
            db.update_item(item_id, **fields)
        return db.get_item(item_id)

    @r.delete("/items/{item_id}")
    def delete_item(item_id: int):
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "물품을 찾을 수 없습니다.")
        for p in (item["photo_path"], item["patch_path"]):
            if p:
                Path(p).unlink(missing_ok=True)
        watcher.remove_track(item_id)
        db.delete_item(item_id)
        db.add_event("item_deleted", f"'{item['name']}' (#{item_id}) 기록을 삭제했습니다.")
        return {"ok": True}

    @r.post("/items/{item_id}/reclassify")
    def reclassify(item_id: int):
        if not items.reclassify(item_id):
            raise HTTPException(400, "재분석할 수 없습니다. (사진 없음)")
        return {"ok": True}

    @r.post("/items/manual")
    async def manual_register(photo: UploadFile):
        data = await photo.read()
        if not data or len(data) > 15 * 1024 * 1024:
            raise HTTPException(400, "사진 파일이 비어 있거나 15MB를 초과합니다.")
        try:
            item_id = items.register_manual(data)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "item_id": item_id}

    @r.get("/items/{item_id}/photo")
    def item_photo(item_id: int):
        item = db.get_item(item_id)
        if not item or not item["photo_path"] or not Path(item["photo_path"]).is_file():
            raise HTTPException(404, "사진이 없습니다.")
        return FileResponse(item["photo_path"], media_type="image/jpeg")

    # ── dashboard ────────────────────────────────────────────
    @r.get("/stats")
    def stats():
        return db.stats()

    @r.get("/events")
    def events(limit: int = 80):
        return {"events": db.list_events(min(limit, 300))}

    @r.get("/status")
    def status():
        s = watcher.status()
        s["ai_provider"] = resolve_provider()
        s["email_configured"] = bool(
            config.get("smtp_user") and config.get("smtp_password") and config.get("admin_email")
        )
        s["scheduler_last_run"] = scheduler.last_run
        s["warn_before_days"] = config.get_int("warn_before_days")
        s["server_time"] = datetime.now().isoformat(timespec="seconds")
        return s

    # ── camera ───────────────────────────────────────────────
    # 여러 브라우저가 동시에 봐도 프레임당 한 번만 JPEG 인코딩하도록 공유 캐시
    enc_cache = {"seq": -1, "data": b""}
    enc_lock = threading.Lock()

    def _encode_latest() -> bytes:
        frame, seq = camera.latest()
        if frame is None or not camera.connected:
            return b""
        with enc_lock:
            if seq == enc_cache["seq"]:
                return enc_cache["data"]
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not ok:
                return b""
            enc_cache["seq"] = seq
            enc_cache["data"] = jpeg.tobytes()
            return enc_cache["data"]

    @r.get("/stream")
    async def stream():
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"

        async def gen():
            no_cam = _no_camera_jpeg()
            while True:
                data = await asyncio.to_thread(_encode_latest)  # 인코딩이 이벤트 루프를 막지 않게
                yield boundary + (data or no_cam) + b"\r\n"
                await asyncio.sleep(0.07)  # ~14fps

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @r.get("/snapshot")
    def snapshot():
        frame, _ = camera.latest()
        if frame is None or not camera.connected:
            data = _no_camera_jpeg()
        else:
            _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            data = jpeg.tobytes()
        return StreamingResponse(iter([data]), media_type="image/jpeg")

    @r.post("/camera/rebaseline")
    def rebaseline():
        watcher.rebaseline()
        db.add_event("rebaseline", "관리자가 기준 화면을 수동으로 초기화했습니다.")
        return {"ok": True}

    @r.post("/camera/pause")
    def pause(body: PauseBody):
        watcher.paused = body.paused
        db.add_event("detector_paused" if body.paused else "detector_resumed",
                     "감지를 일시정지했습니다." if body.paused else "감지를 재개했습니다.")
        return {"ok": True, "paused": watcher.paused}

    # ── settings ─────────────────────────────────────────────
    @r.get("/settings")
    def get_settings():
        return {"settings": config.snapshot(mask_secrets=True)}

    @r.put("/settings")
    def put_settings(body: SettingsPut):
        values = dict(body.values)
        # 마스킹된 비밀값("********xxxx")이 그대로 돌아오면 변경하지 않음
        for key in SECRET_KEYS:
            if key in values and values[key].startswith("********"):
                values.pop(key)
        old_source = config.get("camera_source")
        config.set_many(values)
        # 실제로 소스가 바뀐 경우에만 재연결·기준 초기화 (무관한 설정 저장이
        # 진행 중인 감지 사이클을 끊지 않도록)
        if config.get("camera_source") != old_source:
            camera.set_source(config.get("camera_source"))
            watcher.rebaseline()
        db.add_event("settings_changed", "관리자가 설정을 변경했습니다.")
        return {"settings": config.snapshot(mask_secrets=True)}

    @r.post("/settings/test-email")
    def test_email():
        ok, msg = send_email(
            "[분실물 알림] 테스트 메일",
            "<p>분실물 감지 AI 시스템의 메일 설정이 정상입니다. ✅</p>",
        )
        return {"ok": ok, "message": msg}

    @r.post("/scheduler/run-now")
    def run_scheduler():
        return scheduler.run_once()

    return r
