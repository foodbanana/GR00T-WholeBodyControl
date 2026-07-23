"""5557 g1_debug 스트림에서 엄지(0-2) 관절이 상수로 나가는지 실측 검증.

사용법 (deploy 실행 중에, DGX에서):
    .venv_data_collection/bin/python check_thumb_freeze.py

- last_*_hand_action 엄지 std == 0 이어야 함 (freeze 래치값 그대로)
- left/right_hand_q 엄지 std ~ 1e-3 이하 (실측 엔코더 노이즈만)
- 검증 중 트리거를 당겼다 놓으면 나머지(3-6) std가 커지는 것도 함께 확인 가능
"""
import time

import msgpack
import numpy as np
import zmq

DURATION_SEC = 5.0

ctx = zmq.Context()
s = ctx.socket(zmq.SUB)
s.connect("tcp://localhost:5557")
s.setsockopt_string(zmq.SUBSCRIBE, "g1_debug")
s.setsockopt(zmq.RCVTIMEO, 3000)

prefix = b"g1_debug"
keys = ["last_left_hand_action", "last_right_hand_action", "left_hand_q", "right_hand_q"]
data = {k: [] for k in keys}

t0 = time.time()
n = 0
try:
    while time.time() - t0 < DURATION_SEC:
        raw = s.recv()
        d = msgpack.unpackb(raw[len(prefix):], raw=False)
        for k in keys:
            data[k].append(d[k])
        n += 1
except zmq.Again:
    pass

print(f"수신 프레임: {n} ({n / max(time.time() - t0, 1e-9):.1f} Hz)")
if n == 0:
    print("메시지 없음 — deploy가 실행 중인지, Init done 이후인지 확인")
else:
    for k in keys:
        arr = np.array(data[k])  # (n, 7)
        thumb, rest = arr[:, :3], arr[:, 3:]
        # 명령(last_*_hand_action)은 래치값 그대로라 비트 단위 상수여야 하고,
        # 실측(*_hand_q)은 모터 PD 강성 한계·기구 커플링으로 수 mrad 흔들림이
        # 정상이다 (실측 2026-07-23: 최대 7e-3 rad = 0.4도).
        thresh = 1e-9 if "action" in k else 0.02
        frozen = "✅ 상수" if thumb.std(axis=0).max() < thresh else "❌ 변동 있음"
        print(f"\n{k}:  엄지 판정 {frozen} (기준 std < {thresh})")
        print(f"  엄지(0-2)  std = {thumb.std(axis=0)}")
        print(f"  엄지(0-2)  값  = {thumb[0]}")
        print(f"  나머지(3-6) std = {rest.std(axis=0)}")
