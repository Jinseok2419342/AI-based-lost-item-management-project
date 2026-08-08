"""설정 관리.

우선순위: DB(웹 설정 페이지에서 저장한 값) > .env / 환경변수 > 기본값.
DB 값은 빈 문자열이면 "미설정"으로 취급하고 다음 순위로 넘어간다.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LOSTFOUND_DATA_DIR", BASE_DIR / "data"))
PHOTO_DIR = DATA_DIR / "photos"
PATCH_DIR = DATA_DIR / "patches"
DB_PATH = DATA_DIR / "lostfound.db"

load_dotenv(BASE_DIR / ".env")

for _d in (DATA_DIR, PHOTO_DIR, PATCH_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 설정 키 정의: (키, 기본값, 대응 환경변수)
_DEFS: dict[str, tuple[str, str | None]] = {
    "ai_provider": ("auto", "AI_PROVIDER"),          # auto | openai | gemini | mock
    "openai_api_key": ("", "OPENAI_API_KEY"),
    "gemini_api_key": ("", "GEMINI_API_KEY"),
    "openai_model": ("gpt-4o-mini", "OPENAI_MODEL"),
    "gemini_model": ("gemini-2.0-flash", "GEMINI_MODEL"),

    "days_valuable": ("90", None),                   # 귀중품 보관일
    "days_general": ("60", None),                    # 일반 물품 보관일
    "days_food": ("1", None),                        # 음식 보관일
    "warn_before_days": ("3", None),                 # 폐기 며칠 전 미리 알림

    "email_enabled": ("1", None),
    "admin_email": ("", "ADMIN_EMAIL"),
    "smtp_host": ("smtp.gmail.com", "SMTP_HOST"),
    "smtp_port": ("587", "SMTP_PORT"),
    "smtp_user": ("", "SMTP_USER"),
    "smtp_password": ("", "SMTP_PASSWORD"),

    "camera_source": ("0", "CAMERA_SOURCE"),
    "settle_seconds": ("3.0", None),                 # 움직임 멈춤 후 분석까지 대기(초)
    "motion_threshold": ("0.004", None),             # 움직임 감지 민감도(변화 픽셀 비율)
    "min_area_ratio": ("0.002", None),               # 물체로 인정할 최소 면적 비율
    "max_change_ratio": ("0.45", None),              # 이 이상 바뀌면 조명변화 등으로 보고 기준만 갱신
    "match_threshold": ("0.5", None),                # 물체 존재 판단 템플릿 매칭 점수
}

# 설정 페이지에서 수정 가능한 키 (그 외 키는 PUT /api/settings 에서 무시)
EDITABLE_KEYS = set(_DEFS.keys())
SECRET_KEYS = {"openai_api_key", "gemini_api_key", "smtp_password"}


class Config:
    """DB 오버레이를 가진 설정 저장소. database.Database 주입 후 사용."""

    def __init__(self) -> None:
        self._db = None
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def attach_db(self, db) -> None:
        with self._lock:
            self._db = db
            self._cache = db.get_all_settings()

    def get(self, key: str) -> str:
        default, env_name = _DEFS.get(key, ("", None))
        with self._lock:
            v = self._cache.get(key, "")
        if v != "":
            return v
        if env_name:
            ev = os.environ.get(env_name, "")
            if ev != "":
                return ev
        return default

    def get_int(self, key: str) -> int:
        try:
            return int(float(self.get(key)))
        except ValueError:
            return int(float(_DEFS[key][0]))

    def get_float(self, key: str) -> float:
        try:
            return float(self.get(key))
        except ValueError:
            return float(_DEFS[key][0])

    def get_bool(self, key: str) -> bool:
        return self.get(key).strip().lower() in ("1", "true", "on", "yes")

    def set_many(self, values: dict[str, str]) -> None:
        clean = {k: str(v) for k, v in values.items() if k in EDITABLE_KEYS}
        with self._lock:
            if self._db is None:
                raise RuntimeError("config: DB not attached")
            self._db.set_settings(clean)
            self._cache.update(clean)

    def snapshot(self, mask_secrets: bool = True) -> dict[str, str]:
        out: dict[str, str] = {}
        for key in _DEFS:
            v = self.get(key)
            if mask_secrets and key in SECRET_KEYS and v:
                v = "*" * 8 + v[-4:] if len(v) > 4 else "*" * 8
            out[key] = v
        return out

    def days_for_category(self, category: str) -> int:
        return {
            "valuable": self.get_int("days_valuable"),
            "general": self.get_int("days_general"),
            "food": self.get_int("days_food"),
        }.get(category, self.get_int("days_general"))


config = Config()
