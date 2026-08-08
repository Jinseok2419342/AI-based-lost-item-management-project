"""폐기 기한 스케줄러.

60초마다 보관 중 물품을 검사해
  - 기한 warn_before_days일 전: 사전 알림 메일 (1회)
  - 기한 경과: 폐기 요청 메일 (1회)
을 관리자에게 발송한다.

발송 성공 시에만 아이템별 플래그(warn_sent/expire_sent)를 기록해 중복을 막고,
발송 실패(SMTP 오류 등) 시엔 플래그를 남겨 30분 뒤 재시도한다.
이메일이 미설정/비활성이면 발송을 시도하지 않는다 — 나중에 설정하면 그때 밀린 알림이 나간다.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from ..config import config
from ..database import Database
from .notifier import item_email_html, send_email

log = logging.getLogger("scheduler")

CHECK_INTERVAL = 60.0
RETRY_INTERVAL = 1800.0  # 발송 실패 시 재시도 간격(초)


class DeadlineScheduler:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._retry_after: dict[tuple[int, str], float] = {}  # (item_id, kind) → 재시도 가능 시각
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

    def _email_ready(self) -> bool:
        return config.get_bool("email_enabled") and bool(
            config.get("smtp_host")
            and config.get("smtp_user")
            and config.get("smtp_password")
            and config.get("admin_email")
        )

    def _send_for(self, item: dict, kind: str) -> bool:
        """kind: warn | expired. 성공 시에만 플래그 기록, 실패 시 재시도 예약."""
        key = (item["id"], kind)
        if time.monotonic() < self._retry_after.get(key, 0.0):
            return False
        subject, html = item_email_html(item, kind)
        ok, msg = send_email(subject, html, item["photo_path"])
        flag = "warn_sent" if kind == "warn" else "expire_sent"
        label = "폐기 예정 사전 알림" if kind == "warn" else "폐기 기한 도래 알림"
        if ok:
            self.db.update_item(item["id"], **{flag: 1})
            self._retry_after.pop(key, None)
            self.db.add_event("email_sent", f"'{item['name']}' {label} — {msg}", item["id"])
        else:
            self._retry_after[key] = time.monotonic() + RETRY_INTERVAL
            self.db.add_event(
                "email_failed",
                f"'{item['name']}' {label} 발송 실패 — {msg} (30분 후 재시도)",
                item["id"],
            )
        return ok

    def run_once(self) -> dict:
        """검사 1회 실행. 결과 요약 반환 (수동 트리거 API에서도 사용).

        스케줄러 스레드와 '지금 검사' 버튼이 동시에 돌면 같은 메일이 두 번 나가므로
        논블로킹 락으로 직렬화한다.
        """
        if not self._run_lock.acquire(blocking=False):
            return {
                "checked_at": self.last_run,
                "warn_sent": 0,
                "expire_sent": 0,
                "skipped": "already_running",
            }
        try:
            now = datetime.now()
            self.last_run = now.isoformat(timespec="seconds")
            warn_days = max(0, config.get_int("warn_before_days"))
            sent_warn = sent_expire = 0
            email_ready = self._email_ready()

            for item in self.db.stored_items():
                try:
                    deadline = datetime.fromisoformat(item["deadline"])
                except (ValueError, TypeError):
                    continue
                if not email_ready:
                    continue  # 설정되면 그때 발송 (플래그를 소모하지 않음)

                if now >= deadline and not item["expire_sent"]:
                    if self._send_for(item, "expired"):
                        sent_expire += 1
                elif (
                    deadline - timedelta(days=warn_days) <= now < deadline
                    and not item["warn_sent"]
                ):
                    if self._send_for(item, "warn"):
                        sent_warn += 1

            return {
                "checked_at": self.last_run,
                "warn_sent": sent_warn,
                "expire_sent": sent_expire,
                "email_ready": email_ready,
            }
        finally:
            self._run_lock.release()
