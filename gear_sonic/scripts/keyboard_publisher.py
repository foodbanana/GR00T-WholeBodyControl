#!/usr/bin/env python3
"""제어 키를 ZMQ 로 발행하는 독립 실행 키보드 publisher.

`launch_inference.py` / `launch_data_collection.py` 는 이 기능을 tmux pane 안에서
base64 인라인 스크립트로 띄운다. tmux 없이 **터미널을 직접 여러 개 띄워서** 운용할
때 쓰라고 같은 동작을 파일로 빼놓은 것이다.

C++ deploy(`gear_sonic_deploy`)와 `run_vla_inference.py` 가 **둘 다 이 포트를
구독**한다. 따라서 여기에 키를 입력하면 양쪽이 동시에 반응한다.

사용:
    source .venv_inference/bin/activate      # 또는 .venv_data_collection
    python gear_sonic/scripts/keyboard_publisher.py

키:
    k          제어루프 시작 / 정지
    i          초기 자세로 블렌딩 (POSE 모드)
    p          추론 루프 일시정지 / 재개
    [  ]       손 열기 / 닫기 토글
    t <문장>   언어 프롬프트 교체 (실행 중에도 가능)
    c          (데이터 수집용) 녹화 시작 / 정지
    x          (데이터 수집용) 현재 에피소드 폐기
    Ctrl-C     종료
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import zmq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.data_collection.keyboard_subscriber import (  # noqa: E402
    DEFAULT_ZMQ_KEYBOARD_PORT,
)

HELP = (
    "k=start/stop  i=init pose  p=pause/resume  [ ]=hands  "
    "t <text>=prompt  c=record  x=discard  Ctrl-C=quit"
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="*", help="bind 주소 (기본 *: 모든 인터페이스)")
    ap.add_argument("--port", type=int, default=DEFAULT_ZMQ_KEYBOARD_PORT)
    cfg = ap.parse_args()

    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://{cfg.host}:{cfg.port}")
    # PUB 소켓은 bind 직후 바로 보내면 구독자가 붙기 전이라 메시지가 유실된다.
    time.sleep(0.5)

    print(f"[keyboard] tcp://{cfg.host}:{cfg.port} 에서 대기 중")
    print(f"[keyboard] {HELP}")
    try:
        while True:
            key = input()
            if not key:
                continue
            if key.startswith("t "):
                pub.send_string("prompt:" + key[2:])
                print(f"  → prompt: {key[2:]}")
            else:
                pub.send_string(key)
                print(f"  → {key}")
    except (KeyboardInterrupt, EOFError):
        print("\n[keyboard] 종료")
    finally:
        pub.close()
        ctx.term()


if __name__ == "__main__":
    main()
