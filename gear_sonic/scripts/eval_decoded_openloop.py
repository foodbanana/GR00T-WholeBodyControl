#!/usr/bin/env python3
"""Phase 1/2 — 예측 motion_token을 WBC decoder로 디코드해 관절공간에서 평가한다.

Phase 0(`eval_decoded_gate.py`)에서 채택한 50Hz 보간 + teacher forcing 방식으로,
`token_state` 만 모델 예측값으로 바꿔 decoder를 돌린다. history는 매 프레임
데이터셋 GT에서 새로 만들므로 오차가 누적되지 않는다.

기준(GT)은 `action.wbc`(관절목표, rad, mujoco 순서)이고, 플롯에는
`observation.state`(실제 관절각)도 같이 그린다.

사용:
    scratchpad/onnxenv/bin/python gear_sonic/scripts/eval_decoded_openloop.py \\
        --dataset outputs/raise_arm_banana_val5 \\
        --preds-dir <preds> --out outputs/eval_decoded
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_decoded_gate import load_episode, run_decoder  # noqa: E402
from wbc_decoder import (  # noqa: E402
    JOINT_NAMES, NEGATIVE_EPISODES, RIGHT_ARM, episode_kind,
    isaaclab_action_to_mujoco_target, upsample_states,
)

DEFAULT_DECODER = "gear_sonic_deploy/policy/release/model_decoder.onnx"


def decode_series(sess, ep: dict, tokens: np.ndarray, fps: int) -> np.ndarray:
    """주어진 토큰 시계열로 관절목표(rad, mujoco)를 디코드한다."""
    st, idx = upsample_states(ep["q"], ep["wbc"], ep["quat"], ep["token"], 1.0 / fps, 2)
    n = len(tokens)
    out = np.empty((n, 29))
    for t in range(n):
        out[t] = isaaclab_action_to_mujoco_target(
            run_decoder(sess, st.obs_at(int(idx[t]), token=tokens[t]))
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--preds-dir", type=Path, required=True)
    ap.add_argument("--decoder", type=Path, default=Path(DEFAULT_DECODER))
    ap.add_argument("--checkpoints", nargs="+", type=int, default=None,
                    help="생략하면 --preds-dir 의 ck* 디렉토리에서 자동으로 찾는다")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--warmup", type=int, default=10)
    cfg = ap.parse_args()

    fps = json.loads((cfg.dataset / "meta" / "info.json").read_text())["fps"]
    files = sorted((cfg.dataset / "data" / "chunk-000").glob("*.parquet"))
    sess = ort.InferenceSession(str(cfg.decoder), providers=["CPUExecutionProvider"])
    cfg.out.mkdir(parents=True, exist_ok=True)

    if cfg.checkpoints is None:
        cfg.checkpoints = sorted(int(d.name[2:]) for d in cfg.preds_dir.glob("ck*") if d.is_dir())
        if not cfg.checkpoints:
            sys.exit(f"✗ {cfg.preds_dir} 에 ck* 디렉토리가 없다")
        print(f"체크포인트 자동 탐지: {cfg.checkpoints}")

    # 양성/음성은 `wbc_decoder.NEGATIVE_EPISODES` 목록으로 판정한다(자동판정 아님).
    ep_index = [int(f.stem.split("_")[-1]) for f in files]
    episodes = {k: load_episode(f) for k, f in enumerate(files)}
    kinds = {k: ("negative" if episode_kind(ep_index[k]) == "NEG" else "positive")
             for k in episodes}
    print("에피소드:", {ep_index[k]: kinds[k] for k in episodes})
    print(f"  (음성 목록 {sorted(NEGATIVE_EPISODES)} 중 이 데이터셋에 있는 것: "
          f"{sorted(e for e in ep_index if e in NEGATIVE_EPISODES)})")

    rows = []
    store: dict = {}
    for ck in cfg.checkpoints:
        for k, ep in episodes.items():
            d = np.load(cfg.preds_dir / f"ck{ck}" / f"traj_{k}.npz")
            pred_tok = d["pred"].astype(np.float64)
            n = len(pred_tok)
            assert np.abs(d["gt"] - ep["token"][:n]).max() < 1e-9, \
                f"토큰 정합 실패: ck{ck} traj{k}"

            pred_q = decode_series(sess, ep, pred_tok, fps)
            w = cfg.warmup
            err = pred_q[w:] - ep["wbc"][w:n]
            store[(ck, k)] = pred_q

            rows.append({
                "checkpoint": ck, "traj": k, "episode": ep_index[k], "kind": kinds[k],
                "n_frames": n - w,
                "mse_all_rad2": float((err ** 2).mean()),
                "rmse_all_deg": float(np.degrees(np.sqrt((err ** 2).mean()))),
                "mse_rightarm_rad2": float((err[:, RIGHT_ARM] ** 2).mean()),
                "rmse_rightarm_deg": float(np.degrees(np.sqrt((err[:, RIGHT_ARM] ** 2).mean()))),
                "max_rightarm_deg": float(np.degrees(np.abs(err[:, RIGHT_ARM]).max())),
                **{f"rmse_{JOINT_NAMES[j]}_deg": float(np.degrees(np.sqrt((err[:, j] ** 2).mean())))
                   for j in RIGHT_ARM},
            })
        print(f"  ck{ck} 완료")

    with open(cfg.out / "metrics.csv", "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    np.savez_compressed(
        cfg.out / "decoded.npz",
        # 플롯 스크립트가 에피소드 번호·체크포인트 목록을 하드코딩하지 않도록 같이 담는다.
        ep_index=np.array(ep_index),
        checkpoints=np.array(cfg.checkpoints),
        **{f"ck{ck}_traj{k}": v for (ck, k), v in store.items()},
        **{f"gt_wbc_traj{k}": episodes[k]["wbc"] for k in episodes},
        **{f"gt_state_traj{k}": episodes[k]["q"] for k in episodes},
    )

    print("\n=== 체크포인트별 오른팔 RMSE (도) ===")
    # 에피소드 개수는 데이터셋마다 다르므로 헤더도 실제 개수로 찍는다.
    n_pos = sum(1 for k in kinds.values() if k == "positive")
    n_neg = len(kinds) - n_pos
    print(f"{'ckpt':>7} {f'양성{n_pos}ep':>10} {f'음성{n_neg}ep':>10} {'전체29관절(양성)':>18}")
    for ck in cfg.checkpoints:
        r = [x for x in rows if x["checkpoint"] == ck]
        pos = [x for x in r if x["kind"] == "positive"]
        neg = [x for x in r if x["kind"] == "negative"]
        f = lambda s, key: np.degrees(np.sqrt(np.mean([x[key] for x in s])))  # noqa: E731
        print(f"{ck:>7} {f(pos,'mse_rightarm_rad2'):>10.2f} {f(neg,'mse_rightarm_rad2'):>10.2f} "
              f"{f(pos,'mse_all_rad2'):>18.2f}")

    print(f"\n저장: {cfg.out}/metrics.csv, {cfg.out}/decoded.npz")


if __name__ == "__main__":
    main()
