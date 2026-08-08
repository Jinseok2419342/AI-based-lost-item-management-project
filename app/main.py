"""분실물 감지 AI 시스템 — FastAPI 앱 조립."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import create_router
from .config import config
from .database import Database
from .services.items import ItemService
from .services.scheduler import DeadlineScheduler
from .vision.camera import Camera
from .vision.detector import DetectorCallbacks, SceneWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_app() -> FastAPI:
    db = Database()
    config.attach_db(db)

    items = ItemService(db)
    camera = Camera(config.get("camera_source"))
    watcher = SceneWatcher(
        camera,
        DetectorCallbacks(
            on_new_object=items.register_from_camera,
            on_object_removed=items.mark_removed_by_camera,
            on_event=items.log_event,
        ),
    )
    items.attach_detector(watcher)
    scheduler = DeadlineScheduler(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        camera.start()
        watcher.start()
        scheduler.start()
        log.info("시스템 시작 — 카메라 소스: %s", camera.source)
        db.add_event("system", "시스템이 시작되었습니다.")
        yield
        watcher.stop()
        camera.stop()
        scheduler.stop()
        items.shutdown()
        log.info("시스템 종료")

    app = FastAPI(title="분실물 감지 AI 시스템", lifespan=lifespan)
    app.include_router(create_router(db, items, camera, watcher, scheduler))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = build_app()
