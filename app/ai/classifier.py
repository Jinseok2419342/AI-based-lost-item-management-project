"""멀티모달 AI로 물체 식별.

공급자: openai(GPT) | gemini | mock. SDK 없이 REST 직접 호출(의존성 최소화).
반환 형식은 공급자와 무관하게 동일한 dict.
"""
from __future__ import annotations

import base64
import json
import logging
import re

import requests

from ..config import config

log = logging.getLogger("ai")

CATEGORIES = ("valuable", "general", "food", "ignore")

_PROMPT = """당신은 분실물 보관소의 물품 식별 AI입니다. 사진 속 물체를 식별해 JSON으로만 답하세요.

규칙:
- "category"는 반드시 다음 중 하나: "valuable"(지갑·휴대폰·노트북·귀금속·시계·이어폰 등 고가품), "general"(우산·의류·텀블러·책·필기구 등 일반 물품), "food"(음식·음료·과일 등 부패 가능한 것), "ignore"(사람·신체 일부·그림자·빛 반사 등 물건이 아닌 것)
- "name": 한국어로 간단한 물품명 (예: "검은색 가죽 지갑")
- "description": 한국어 한 문장 설명 (색상, 브랜드, 특징)
- "confidence": 0.0~1.0 확신도

JSON 형식 예시:
{"name": "검은색 가죽 지갑", "category": "valuable", "description": "카드 슬롯이 보이는 검은색 가죽 반지갑입니다.", "confidence": 0.9}"""


def _fallback(reason: str) -> dict:
    return {
        "name": "미확인 물품",
        "category": "general",
        "description": f"AI 분석 실패로 일반 물품으로 등록되었습니다. ({reason})",
        "confidence": 0.0,
        "provider": "fallback",
        "ok": False,
    }


def _parse_json(text: str) -> dict | None:
    """모델 응답에서 JSON 오브젝트 추출 (코드펜스/잡담 허용)."""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "미확인 물품").strip()[:80]
    category = str(data.get("category") or "general").strip().lower()
    if category not in CATEGORIES:
        category = "general"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "name": name,
        "category": category,
        "description": str(data.get("description") or "").strip()[:300],
        "confidence": confidence,
    }


def _classify_openai(jpeg: bytes, api_key: str) -> dict:
    b64 = base64.b64encode(jpeg).decode()
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": config.get("openai_model"),
            "response_format": {"type": "json_object"},
            "max_tokens": 300,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
        },
        timeout=45,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    parsed = _parse_json(text)
    if not parsed:
        raise ValueError(f"JSON 파싱 실패: {text[:120]}")
    return {**parsed, "provider": "openai", "ok": True}


def _classify_gemini(jpeg: bytes, api_key: str) -> dict:
    b64 = base64.b64encode(jpeg).decode()
    model = config.get("gemini_model")
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": _PROMPT},
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": 300,
            },
        },
        timeout=45,
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    parsed = _parse_json(text)
    if not parsed:
        raise ValueError(f"JSON 파싱 실패: {text[:120]}")
    return {**parsed, "provider": "gemini", "ok": True}


def _classify_mock(jpeg: bytes) -> dict:
    return {
        "name": "미확인 물품",
        "category": "general",
        "description": "mock 모드로 등록된 물품입니다. 설정에서 AI API 키를 입력하면 자동 식별됩니다.",
        "confidence": 0.3,
        "provider": "mock",
        "ok": True,
    }


def resolve_provider() -> str:
    """auto 모드면 사용 가능한 공급자 선택."""
    p = config.get("ai_provider").strip().lower()
    if p == "openai":
        return "openai" if config.get("openai_api_key") else "mock"
    if p == "gemini":
        return "gemini" if config.get("gemini_api_key") else "mock"
    if p == "mock":
        return "mock"
    # auto
    if config.get("openai_api_key"):
        return "openai"
    if config.get("gemini_api_key"):
        return "gemini"
    return "mock"


def classify_image(jpeg: bytes) -> dict:
    """물체 사진(JPEG bytes) → {name, category, description, confidence, provider, ok}.

    예외를 던지지 않는다. 실패 시 fallback dict 반환.
    """
    provider = resolve_provider()
    try:
        if provider == "openai":
            return _classify_openai(jpeg, config.get("openai_api_key"))
        if provider == "gemini":
            return _classify_gemini(jpeg, config.get("gemini_api_key"))
        return _classify_mock(jpeg)
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 시스템은 계속 동작해야 함
        log.warning("AI 분석 실패(%s): %s", provider, e)
        return _fallback(str(e)[:100])
