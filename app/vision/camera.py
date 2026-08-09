"""카메라 캡처 스레드.

- 소스: 웹캠 번호("0") 또는 동영상 파일 경로. 파일이면 FPS에 맞춰 재생하고 끝나면 반복.
- 연결이 끊기면 3초 간격으로 자동 재연결.
- 모든 소비자(감지기, 스트림, 스냅샷)는 width<=960 으로 리사이즈된 최신 프레임을 공유한다.
"""
from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np

log = logging.getLogger("camera")

PROC_WIDTH = 960

_LIVE_PREFIXES = ("rtsp://", "rtsps://", "http://", "https://", "/dev/")


def _source_is_file(source: str) -> bool:
    """파일(반복 재생·FPS 페이싱 대상)인지, 라이브 소스인지 구분."""
    s = source.strip()
    return not (s.isdigit() or s.lower().startswith(_LIVE_PREFIXES))


class Camera:
    def __init__(self, source: str) -> None:
        self.source = source
        self._cap: cv2.VideoCapture | None = None
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_file = _source_is_file(source)
        self._reopen = False

    # ── lifecycle ────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                return  # 스레드가 아직 read() 중 — 다른 스레드에서 release()하면 크래시 위험
        if self._cap is not None:
            self._cap.release()

    def set_source(self, source: str) -> None:
        """설정 변경 시 소스 교체. 실제 재연결은 카메라 스레드가 수행한다.

        (VideoCapture의 release/read를 서로 다른 스레드에서 동시에 부르면
        네이티브 크래시가 날 수 있어, 여기서는 플래그만 세운다.)
        """
        if source == self.source:
            return
        self.source = source
        self._is_file = _source_is_file(source)
        self._reopen = True

    # ── consumers ────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    def latest(self) -> tuple[np.ndarray | None, int]:
        with self._lock:
            if self._frame is None:
                return None, self._seq
            return self._frame.copy(), self._seq

    # ── internals ────────────────────────────────────────────
    def _release(self) -> None:
        cap, self._cap = self._cap, None
        self._connected = False
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def _open(self) -> bool:
        src = self.source.strip()
        cap = cv2.VideoCapture(int(src) if src.isdigit() else src)
        if not cap.isOpened():
            cap.release()
            return False
        if not self._is_file:
            # 웹캠 내부 버퍼를 줄여 화면 지연 최소화 (미지원 백엔드에선 무시됨)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        self._connected = True
        log.info("카메라 연결됨: %s", src)
        return True

    def _run(self) -> None:
        next_retry = 0.0
        frame_interval = 0.0
        while not self._stop.is_set():
            try:
                if self._reopen:
                    self._reopen = False
                    self._release()
                    next_retry = 0.0

                cap = self._cap
                if cap is None:
                    if time.monotonic() < next_retry:
                        time.sleep(0.2)
                        continue
                    if not self._open():
                        next_retry = time.monotonic() + 3.0
                        continue
                    cap = self._cap
                    if self._is_file:
                        fps = cap.get(cv2.CAP_PROP_FPS) or 0
                        frame_interval = 1.0 / fps if 1 <= fps <= 120 else 1.0 / 20

                t0 = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None:
                    if self._is_file:
                        # 파일 끝 → 처음부터 반복 재생 (시연 편의)
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, frame = cap.read()
                    if not ok or frame is None:
                        log.warning("카메라 프레임 읽기 실패, 재연결 시도")
                        self._release()
                        next_retry = time.monotonic() + 3.0
                        continue

                if frame.shape[1] > PROC_WIDTH:
                    h = int(frame.shape[0] * PROC_WIDTH / frame.shape[1])
                    frame = cv2.resize(frame, (PROC_WIDTH, h), interpolation=cv2.INTER_AREA)

                with self._lock:
                    self._frame = frame
                    self._seq += 1

                if self._is_file:
                    elapsed = time.monotonic() - t0
                    delay = frame_interval - elapsed
                    if delay > 0:
                        time.sleep(delay)
            except Exception:  # noqa: BLE001 — 캡처 스레드는 죽으면 안 됨
                log.exception("카메라 루프 오류, 재연결 시도")
                self._release()
                next_retry = time.monotonic() + 3.0
                time.sleep(0.5)
