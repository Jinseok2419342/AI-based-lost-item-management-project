"""시연·테스트용 합성 영상 생성.

시나리오: 빈 책상 → 손이 들어와 지갑을 놓고 나감 → 잠시 후 손이 지갑을 가져감.
사용법: python scripts/make_demo_video.py [출력경로=demo.mp4]
"""
from __future__ import annotations

import sys

import cv2
import numpy as np

W, H, FPS = 640, 480, 20

WALLET_POS = (270, 240)   # (x, y)
WALLET_SIZE = (110, 70)   # (w, h)


def background() -> np.ndarray:
    """나뭇결 느낌의 책상 배경 (프레임마다 동일해야 함)."""
    img = np.zeros((H, W, 3), np.uint8)
    for y in range(H):
        shade = 150 + int(28 * np.sin(y / 37.0))
        img[y, :] = (int(shade * 0.55), int(shade * 0.72), shade)  # BGR 갈색톤
    rng = np.random.RandomState(42)
    for _ in range(60):  # 나뭇결 줄무늬
        x0, y0 = rng.randint(0, W), rng.randint(0, H)
        cv2.line(img, (x0, y0), (x0 + rng.randint(40, 160), y0),
                 (90, 110, 140), 1, cv2.LINE_AA)
    cv2.rectangle(img, (30, 30), (140, 110), (190, 200, 210), -1)  # 고정 소품(메모지)
    cv2.rectangle(img, (30, 30), (140, 110), (120, 130, 150), 2)
    return img


def draw_wallet(img: np.ndarray) -> None:
    x, y = WALLET_POS
    w, h = WALLET_SIZE
    cv2.rectangle(img, (x, y), (x + w, y + h), (30, 45, 80), -1)          # 몸통
    cv2.rectangle(img, (x, y), (x + w, y + h), (15, 25, 50), 3)           # 테두리
    cv2.line(img, (x + 8, y + h // 2), (x + w - 8, y + h // 2), (60, 85, 130), 2)
    cv2.rectangle(img, (x + w - 34, y + 12), (x + w - 12, y + 30), (50, 70, 110), -1)
    cv2.circle(img, (x + 22, y + h - 18), 6, (90, 120, 160), -1)          # 스냅 버튼


def draw_hand(img: np.ndarray, cx: int, cy: int) -> None:
    cv2.ellipse(img, (cx, cy), (55, 38), 15, 0, 360, (110, 140, 190), -1)  # 손바닥
    for i in range(4):
        fx = cx - 40 + i * 24
        cv2.ellipse(img, (fx, cy - 34), (10, 26), 0, 0, 360, (110, 140, 190), -1)
    cv2.ellipse(img, (cx, cy), (55, 38), 15, 0, 360, (80, 105, 150), 2)


def main() -> None:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "demo.mp4"
    bg = background()
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    if not writer.isOpened():
        raise SystemExit("VideoWriter를 열 수 없습니다.")

    wx, wy = WALLET_POS
    hand_target = (wx + WALLET_SIZE[0] // 2, wy + WALLET_SIZE[1] // 2)

    def emit(seconds: float, render) -> None:
        for i in range(int(seconds * FPS)):
            frame = bg.copy()
            render(frame, i / max(1, int(seconds * FPS) - 1))
            writer.write(frame)

    # 1) 빈 책상 5초
    emit(5, lambda f, t: None)
    # 2) 손이 지갑을 들고 들어옴 (1.5초)
    emit(1.5, lambda f, t: draw_hand(f, int(W + 60 - (W + 60 - hand_target[0]) * t),
                                     hand_target[1]))
    # 3) 지갑 내려놓고 손이 빠져나감 (1.2초)
    def leave_after_drop(f, t):
        draw_wallet(f)
        draw_hand(f, int(hand_target[0] + (W + 80 - hand_target[0]) * t), hand_target[1])
    emit(1.2, leave_after_drop)
    # 4) 지갑만 있는 정지 장면 10초 (settle → 등록)
    emit(10, lambda f, t: draw_wallet(f))
    # 5) 손이 다시 들어옴 (1.2초)
    def hand_returns(f, t):
        draw_wallet(f)
        draw_hand(f, int(W + 60 - (W + 60 - hand_target[0]) * t), hand_target[1])
    emit(1.2, hand_returns)
    # 6) 지갑을 집어 나감 (1.2초) — 지갑 없음
    emit(1.2, lambda f, t: draw_hand(f, int(hand_target[0] + (W + 80 - hand_target[0]) * t),
                                     hand_target[1]))
    # 7) 빈 책상 10초 (settle → 회수 감지)
    emit(10, lambda f, t: None)

    writer.release()
    print(f"생성 완료: {out_path} (약 30초, {W}x{H} @ {FPS}fps)")


if __name__ == "__main__":
    main()
