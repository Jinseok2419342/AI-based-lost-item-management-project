"""폐기 기한 스케줄러.

60초마다 보관 중 물품을 검사해
  - 기한 warn_before_days일 전: 사전 알림 메일 (1회)
  - 기한 경과: 폐기 요청 메일 (1회)
을 관리자에게 발송한다. 발송 여부는 아이템별 플래그로 기록해 중복을 막는다.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from ..config import config
from ..database import Database
from .notifier import item_email_html, send_email

log = logging.getLogger("scheduler")

CHECK_INTERVAL = 60.0


class DeadlineScheduler:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        # 서버 기동 직후 잠깐 기다렸다 첫 검사 (DB/설정 로드 안정화)
        self._stop.wait(5)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("스케줄러 오류")
            self._stop.wait(CHECK_INTERVAL)

    def run_once(self) -> dict:
        """검사 1회 실행. 결과 요약 반환 (수동 트리거 API에서도 사용)."""
        now = datetime.now()
        self.last_run = now.isoformat(timespec="seconds")
        warn_days = config.get_int("warn_before_days")
        sent_warn = sent_expire = 0

        for item in self.db.stored_items():
            try:
                deadline = datetime.fromisoformat(item["deadline"])
            except (ValueError, TypeError):
                continue

            if now >= deadline and not item["expire_sent"]:
                subject, html = item_email_html(item, "expired")
                ok, msg = send_email(subject, html, item["photo_path"])
                # 성공 여부와 무관하게 1회만 시도 기록 (설정 미비로 무한 재시도 방지)
                self.db.update_item(item["id"], expire_sent=1)
                self.db.add_event(
                    "email_sent" if ok else "email_failed",
                    f"'{item['name']}' 폐기 기한 도래 알림 — {msg}",
                    item["id"],
                )
                if ok:
                    sent_expire += 1
            elif (
                now >= deadline - timedelta(days=warn_days)
                and now < deadline
                and not item["warn_sent"]
            ):
                subject, html = item_email_html(item, "warn")
                ok, msg = send_email(subject, html, item["photo_path"])
                self.db.update_item(item["id"], warn_sent=1)
                self.db.add_event(
                    "email_sent" if ok else "email_failed",
                    f"'{item['name']}' 폐기 예정({str(item['deadline'])[:10]}) 사전 알림 — {msg}",
                    item["id"],
                )
                if ok:
                    sent_warn += 1

        return {"checked_at": self.last_run, "warn_sent": sent_warn, "expire_sent": sent_expire}
