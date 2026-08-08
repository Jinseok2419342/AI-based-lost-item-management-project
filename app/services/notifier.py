"""관리자 이메일 알림 (SMTP, Gmail 앱 비밀번호 권장)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from ..config import config

log = logging.getLogger("notifier")


def _build_message(subject: str, html: str, photo_path: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.get("smtp_user") or config.get("admin_email")
    msg["To"] = config.get("admin_email")
    msg.set_content("HTML 메일을 지원하는 클라이언트에서 확인해 주세요.")
    msg.add_alternative(html, subtype="html")
    if photo_path:
        p = Path(photo_path)
        if p.is_file():
            msg.add_attachment(
                p.read_bytes(), maintype="image", subtype="jpeg", filename=p.name
            )
    return msg


def send_email(subject: str, html: str, photo_path: str | None = None) -> tuple[bool, str]:
    """(성공 여부, 메시지). 예외를 던지지 않는다."""
    if not config.get_bool("email_enabled"):
        return False, "이메일 알림이 비활성화되어 있습니다."
    host = config.get("smtp_host")
    user = config.get("smtp_user")
    password = config.get("smtp_password")
    to = config.get("admin_email")
    if not (host and user and password and to):
        return False, "SMTP 설정(호스트/계정/앱 비밀번호/관리자 메일)이 비어 있습니다."
    try:
        port = config.get_int("smtp_port")
        msg = _build_message(subject, html, photo_path)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
        return True, f"{to} 로 발송 완료"
    except Exception as e:  # noqa: BLE001
        log.warning("메일 발송 실패: %s", e)
        return False, f"발송 실패: {e}"


_CATEGORY_KO = {"valuable": "귀중품", "general": "일반 물품", "food": "음식"}


def item_email_html(item: dict, kind: str) -> tuple[str, str]:
    """(제목, HTML 본문). kind: warn | expired"""
    cat = _CATEGORY_KO.get(item["category"], item["category"])
    deadline = str(item["deadline"])[:10]
    if kind == "warn":
        subject = f"[분실물 알림] 폐기 예정 안내 — {item['name']} (기한 {deadline})"
        headline = "폐기 기한이 다가오는 분실물이 있습니다."
        color = "#FF9500"
    else:
        subject = f"[분실물 알림] 폐기 기한 도래 — {item['name']}"
        headline = "보관 기한이 지난 분실물이 있습니다. 폐기 처리를 진행해 주세요."
        color = "#FF3B30"
    html = f"""
    <div style="font-family:-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;
                max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="color:{color};margin:0 0 4px;">{headline}</h2>
      <p style="color:#666;margin:0 0 20px;">분실물 감지 AI 시스템 자동 알림</p>
      <table style="border-collapse:collapse;width:100%;background:#f5f5f7;border-radius:12px;">
        <tr><td style="padding:12px 16px;color:#888;width:96px;">물품명</td>
            <td style="padding:12px 16px;font-weight:600;">{item['name']}</td></tr>
        <tr><td style="padding:12px 16px;color:#888;">분류</td>
            <td style="padding:12px 16px;">{cat}</td></tr>
        <tr><td style="padding:12px 16px;color:#888;">등록일</td>
            <td style="padding:12px 16px;">{str(item['registered_at'])[:16].replace('T',' ')}</td></tr>
        <tr><td style="padding:12px 16px;color:#888;">폐기 기한</td>
            <td style="padding:12px 16px;font-weight:600;color:{color};">{deadline}</td></tr>
        <tr><td style="padding:12px 16px;color:#888;">설명</td>
            <td style="padding:12px 16px;">{item['description'] or '—'}</td></tr>
      </table>
      <p style="color:#999;font-size:12px;margin-top:20px;">
        관리자 페이지에서 상태를 변경할 수 있습니다. 물품 사진은 첨부 파일을 확인하세요.
      </p>
    </div>"""
    return subject, html
