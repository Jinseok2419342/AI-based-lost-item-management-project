"""엔드투엔드 자동 테스트.

합성 데모 영상을 카메라 소스로 서버를 띄우고,
  물체 등록 감지 → 회수 감지 → API 동작 → 폐기 스케줄러
가 실제로 작동하는지 검증한다. (AI는 mock 모드, 이메일은 비활성)

사용법: .venv/bin/python scripts/run_e2e_test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def wait_for(desc: str, fn, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except requests.RequestException:
            pass
        time.sleep(interval)
    return None


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="lostfound_e2e_")
    video = str(Path(tmp) / "demo.mp4")
    subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts/make_demo_video.py"), video],
        check=True,
    )

    env = {
        **os.environ,
        "CAMERA_SOURCE": video,
        "AI_PROVIDER": "mock",
        "LOSTFOUND_DATA_DIR": str(Path(tmp) / "data"),
        "PORT": str(PORT),
    }
    server = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "run.py")],
        env=env,
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        print("\n■ 서버 기동 대기")
        st = wait_for(
            "server", lambda: requests.get(f"{BASE}/api/status", timeout=2).json(), 30
        )
        check("서버 기동", st is not None)
        if st is None:
            return finish(server)

        st = wait_for(
            "camera",
            lambda: (lambda s: s if s["camera_connected"] else None)(
                requests.get(f"{BASE}/api/status", timeout=2).json()
            ),
            15,
        )
        check("카메라(영상 소스) 연결", st is not None)

        print("\n■ 물체 등장 감지 (지갑을 놓는 장면)")
        items = wait_for(
            "registered",
            lambda: (lambda d: d["items"] if d["items"] else None)(
                requests.get(f"{BASE}/api/items", timeout=2).json()
            ),
            40,
        )
        check("새 물건 자동 등록", bool(items), f"{len(items or [])}건 등록됨")
        item_id = items[0]["id"] if items else None

        if item_id:
            item = wait_for(
                "classified",
                lambda: (lambda i: i if i["ai_status"] != "pending" else None)(
                    requests.get(f"{BASE}/api/items/{item_id}", timeout=2).json()
                ),
                15,
            )
            check("AI(mock) 분석 완료", item is not None,
                  f"이름='{item['name']}', 분류={item['category']}" if item else "")
            if item:
                dl = datetime.fromisoformat(item["deadline"])
                reg = datetime.fromisoformat(item["registered_at"])
                days = (dl - reg).days
                check("보관 기한 부여(일반=60일)", 59 <= days <= 60, f"{days}일")

        print("\n■ 물체 회수 감지 (지갑을 가져가는 장면)")
        item = wait_for(
            "retrieved",
            lambda: (lambda i: i if i["status"] == "retrieved" else None)(
                requests.get(f"{BASE}/api/items/{item_id}", timeout=2).json()
            ),
            45,
        ) if item_id else None
        check("사라짐 → 자동 회수 처리", item is not None)

        print("\n■ 관리 API")
        r = requests.get(f"{BASE}/api/stats", timeout=2).json()
        check("통계 API", "stored" in r and "retrieved" in r, str(r))
        r = requests.get(f"{BASE}/api/events", timeout=2).json()
        types = {e["type"] for e in r["events"]}
        check("이벤트 로그 기록", {"item_registered", "item_retrieved"} <= types,
              f"types={sorted(types)}")
        r = requests.get(f"{BASE}/api/settings", timeout=2).json()
        check("설정 조회", r["settings"]["days_general"] == "60")
        r = requests.put(f"{BASE}/api/settings",
                         json={"values": {"days_food": "2"}}, timeout=2).json()
        check("설정 저장", r["settings"]["days_food"] == "2")
        r = requests.get(f"{BASE}/api/snapshot", timeout=5)
        check("스냅샷 이미지", r.status_code == 200 and r.content[:2] == b"\xff\xd8")
        if item_id:
            r = requests.get(f"{BASE}/api/items/{item_id}/photo", timeout=2)
            check("물품 사진 제공", r.status_code == 200)

        print("\n■ 폐기 스케줄러")
        if item_id:
            requests.patch(f"{BASE}/api/items/{item_id}",
                           json={"status": "stored"}, timeout=2)
            yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
            r = requests.patch(f"{BASE}/api/items/{item_id}",
                               json={"deadline": yesterday}, timeout=2)
            check("기한 수정 API", r.status_code == 200)
            rr = requests.post(f"{BASE}/api/scheduler/run-now", timeout=10).json()
            check("스케줄러 수동 실행", "checked_at" in rr and rr.get("email_ready") is False)
            st2 = requests.get(f"{BASE}/api/stats", timeout=2).json()
            check("기한 경과 물품 집계", st2["expired"] >= 1, f"expired={st2['expired']}")
            item = requests.get(f"{BASE}/api/items/{item_id}", timeout=2).json()
            check("메일 미설정 시 알림 보류(플래그 미소모)", item["expire_sent"] == 0)

        r = requests.get(f"{BASE}/", timeout=2)
        check("관리자 웹 페이지", r.status_code == 200 and "분실물" in r.text)
        return finish(server)
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


def finish(server: subprocess.Popen) -> int:
    print(f"\n결과: {len(passed)} PASS / {len(failed)} FAIL")
    if failed:
        print("실패 항목:", ", ".join(failed))
        if server.poll() is None:
            server.terminate()
            try:
                out, _ = server.communicate(timeout=5)
                print("\n── 서버 로그(끝부분) ──")
                print("\n".join(out.splitlines()[-40:]))
            except subprocess.TimeoutExpired:
                server.kill()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
