#!/usr/bin/env python3
"""Phase 0 — WBC decoder 오프라인 재현 검증 게이트.

GT motion_token + GT history 를 `model_decoder.onnx` 에 넣은 출력이 데이터셋에
기록된 `action.wbc`(관절목표)를 재현하는지 본다. 재현되면 오프라인 harness가
정확하다는 뜻이고, 이후 token 만 예측값으로 갈아끼우면 open-loop eval이 된다.

두 변형을 비교한다:
  (a) 25Hz  — 데이터셋 프레임을 그대로. history 10프레임이 400ms를 덮는다.
  (b) 50Hz  — 보간해 0.02s 간격 history를 만든다(decoder 원래 분포). 출력은
              원본 프레임 시각에서만 평가.

사용:
    scratchpad/onnxenv/bin/python gear_sonic/scripts/eval_decoded_gate.py \\
        --dataset outputs/raise_arm_banana_val5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wbc_decoder import (  # noqa: E402
    BODY29_IN_43, JOINT_NAMES, RIGHT_ARM, EpisodeStates,
    isaaclab_action_to_mujoco_target, upsample_states,
)

DEFAULT_DECODER = "gear_sonic_deploy/policy/release/model_decoder.onnx"


def load_episode(path: Path) -> dict:
    t = pq.read_table(
        path,
        columns=["observation.state", "action.wbc", "observation.root_orientation",
                 "action.motion_token"],
    )
    arr = lambda c: np.array(t.column(c).to_pylist(), dtype=np.float64)  # noqa: E731
    return {
        "q": arr("observation.state")[:, BODY29_IN_43],
        "wbc": arr("action.wbc")[:, BODY29_IN_43],
        "quat": arr("observation.root_orientation"),
        "token": arr("action.motion_token"),
    }


def run_decoder(sess: ort.InferenceSession, obs: np.ndarray) -> np.ndarray:
    name = sess.get_inputs()[0].name
    return sess.run(None, {name: obs.astype(np.float32)[None, :]})[0][0]


def evaluate(sess, ep: dict, mode: str, fps: int) -> np.ndarray:
    """모드별로 decoder를 돌려 관절목표(mujoco, rad) 예측 시계열을 반환."""
    dt = 1.0 / fps
    if mode == "25hz":
        st = EpisodeStates(ep["q"], ep["wbc"], ep["quat"], ep["token"], dt)
        eval_idx = np.arange(st.n)
    elif mode == "50hz":
        st, eval_idx = upsample_states(ep["q"], ep["wbc"], ep["quat"], ep["token"], dt, 2)
    else:
        raise ValueError(mode)

    out = np.empty((len(eval_idx), 29))
    for k, t in enumerate(eval_idx):
        out[k] = isaaclab_action_to_mujoco_target(run_decoder(sess, st.obs_at(int(t))))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--decoder", type=Path, default=Path(DEFAULT_DECODER))
    ap.add_argument("--modes", nargs="+", default=["25hz", "50hz"])
    ap.add_argument("--warmup", type=int, default=10,
                    help="history가 채워지기 전 앞쪽 N 프레임은 지표에서 제외")
    cfg = ap.parse_args()

    info = json.loads((cfg.dataset / "meta" / "info.json").read_text())
    fps = info["fps"]
    files = sorted((cfg.dataset / "data" / "chunk-000").glob("*.parquet"))
    sess = ort.InferenceSession(str(cfg.decoder), providers=["CPUExecutionProvider"])

    print(f"decoder : {cfg.decoder}")
    print(f"dataset : {cfg.dataset}  ({len(files)} ep, {fps} fps)\n")

    summary: dict[str, list] = {m: [] for m in cfg.modes}
    for f in files:
        ep_idx = int(f.stem.split("_")[-1])
        ep = load_episode(f)
        gt = ep["wbc"]
        line = f"  ep {ep_idx:>3} (n={len(gt):>3})  "
        for mode in cfg.modes:
            pred = evaluate(sess, ep, mode, fps)
            w = cfg.warmup
            err = pred[w:] - gt[w:]
            rmse_all = np.degrees(np.sqrt((err**2).mean()))
            rmse_arm = np.degrees(np.sqrt((err[:, RIGHT_ARM] ** 2).mean()))
            summary[mode].append((err, ep_idx))
            line += f"{mode}: 전체 {rmse_all:6.2f}° 오른팔 {rmse_arm:6.2f}°   "
        print(line)

    print("\n=== 종합 ===")
    for mode in cfg.modes:
        err = np.concatenate([e for e, _ in summary[mode]])
        rmse_all = np.degrees(np.sqrt((err**2).mean()))
        rmse_arm = np.degrees(np.sqrt((err[:, RIGHT_ARM] ** 2).mean()))
        mse_rad2 = (err**2).mean()
        print(f"  {mode:<6} 전체 RMSE {rmse_all:6.3f}°  오른팔 RMSE {rmse_arm:6.3f}°  "
              f"(MSE {mse_rad2:.6f} rad²)")

    best = min(cfg.modes,
               key=lambda m: (np.concatenate([e for e, _ in summary[m]]) ** 2).mean())
    print(f"\n  → 채택: {best}")

    err = np.concatenate([e for e, _ in summary[best]])
    per = np.degrees(np.sqrt((err**2).mean(0)))
    print(f"\n  {best} 관절별 RMSE(°) 최악 8개:")
    for j in np.argsort(-per)[:8]:
        print(f"    {JOINT_NAMES[j]:<18} {per[j]:7.3f}")
    print(f"\n  오른팔 7개:")
    for j in RIGHT_ARM:
        print(f"    {JOINT_NAMES[j]:<18} {per[j]:7.3f}")


if __name__ == "__main__":
    main()
