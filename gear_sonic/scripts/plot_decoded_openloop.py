#!/usr/bin/env python3
"""Phase 2 — decoded joint open-loop eval 플롯.

관절 하나당 3곡선을 겹쳐 그린다:
  - 실제 관절값   `observation.state`
  - GT 관절목표   `action.wbc`
  - 모델 예측     decoder(pred_token)

`eval_decoded_openloop.py` 가 만든 `decoded.npz` 를 읽으므로 decoder 재실행이 없다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wbc_decoder import (  # noqa: E402
    JOINT_LIMITS_DEG, JOINT_NAMES, NEGATIVE_EPISODES, RIGHT_ARM, episode_kind,
)

def arm_range_deg(state: np.ndarray) -> float:
    """오른팔 최대 가동범위(도). 배너에 참고로 표시할 뿐, 판정에는 쓰지 않는다."""
    return float(np.degrees(state[:, RIGHT_ARM].max(0) - state[:, RIGHT_ARM].min(0)).max())


def plot_set(d, ck: int, k: int, ep: int, joints, fps: int, out: Path, title_extra: str,
             curves: str = "all", ylim: str = "limit") -> None:
    pred = d[f"ck{ck}_traj{k}"]
    wbc = d[f"gt_wbc_traj{k}"]
    state = d[f"gt_state_traj{k}"]
    n = len(pred)
    t = np.arange(n) / fps

    ncol = 2
    nrow = int(np.ceil(len(joints) / ncol))
    fig_h = 2.5 * nrow
    fig, axes = plt.subplots(nrow, ncol, figsize=(13, fig_h), sharex=True)
    axes = np.atleast_1d(axes).ravel()

    for ax, j in zip(axes, joints):
        # 범례는 영문 — matplotlib 기본 폰트(DejaVu Sans)에 한글 글리프가 없다.
        if curves in ("all", "state"):
            # 회색(실제 도달각)은 참고선이다. 모델이 추종해야 할 대상은 파랑이므로
            # "all" 에서는 얇고 옅게 그려 주된 비교(파랑 vs 빨강)를 가리지 않게 한다.
            ref = dict(lw=1.2, alpha=0.55) if curves == "all" else dict(lw=1.7)
            ax.plot(t, np.degrees(state[:n, j]), color="0.45",
                    label="actual joint (observation.state)", **ref)
        if curves in ("all", "target"):
            ax.plot(t, np.degrees(wbc[:n, j]), color="tab:blue", lw=1.8,
                    label="GT target (action.wbc)")
        ax.plot(t, np.degrees(pred[:, j]), color="tab:red", lw=1.5, ls="--",
                label="model (decoded pred token)")

        base = wbc if curves != "state" else state
        rmse = np.degrees(np.sqrt(((pred[10:, j] - base[10:n, j]) ** 2).mean()))
        used = np.degrees(np.concatenate([state[:n, j], wbc[:n, j]]))
        ax.set_title(f"{JOINT_NAMES[j]}   RMSE {rmse:.1f}°   "
                     f"(used {used.max() - used.min():.0f}° of "
                     f"{JOINT_LIMITS_DEG[j, 1] - JOINT_LIMITS_DEG[j, 0]:.0f}° range)",
                     fontsize=9)
        if ylim == "limit":
            # 관절 물리 가동한계로 고정 — 그림끼리 y축이 흔들리지 않는다.
            ax.set_ylim(*JOINT_LIMITS_DEG[j])
            ax.axhline(JOINT_LIMITS_DEG[j, 0], color="0.75", lw=0.8, ls=":")
            ax.axhline(JOINT_LIMITS_DEG[j, 1], color="0.75", lw=0.8, ls=":")
        ax.set_ylabel("deg", fontsize=8)
        ax.grid(alpha=0.3)
    for ax in axes[len(joints):]:
        ax.axis("off")
    axes[0].legend(fontsize=8, loc="best")
    for ax in axes[max(0, len(joints) - ncol):len(joints)]:
        ax.set_xlabel("time (s)")

    kind = episode_kind(ep)
    rng = arm_range_deg(state)
    if kind == "NEG":
        banner = ("NEGATIVE episode  —  no banana, the arm is SUPPOSED to stay still "
                  f"(right-arm range only {rng:.1f}deg).  Low RMSE here is expected, not skill.")
        color, bg = "#7a3b00", "#ffe8cc"
    else:
        banner = ("POSITIVE episode  —  banana present, the arm should be raised "
                  f"(right-arm range {rng:.0f}deg).")
        color, bg = "#14532d", "#d9f2e0"

    # 위 여백은 figure 높이(인치)에 맞춰 잡는다. 상대좌표로 고정하면 29관절 그림처럼
    # 세로로 긴 figure 에서 간격이 과하게 벌어지거나 글씨가 겹친다.
    fig.tight_layout(rect=(0, 0, 1, 1 - 0.80 / fig_h))
    fig.text(0.5, 1 - 0.22 / fig_h, banner, ha="center", va="center",
             fontsize=11, color=color, weight="bold",
             bbox=dict(boxstyle="round,pad=0.35", facecolor=bg, edgecolor=color, linewidth=0.8))
    fig.text(0.5, 1 - 0.58 / fig_h, f"ck{ck}   ep{ep}   {title_extra}",
             ha="center", va="center", fontsize=12)
    fig.savefig(out, dpi=110)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decoded", type=Path,
                    default=Path("eval_results/decoded_openloop_20260806_h10/data/decoded.npz"))
    ap.add_argument("--out", type=Path, default=Path("eval_results/decoded_openloop_20260806_h10"))
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--checkpoints", nargs="+", type=int, default=None,
                    help="생략하면 decoded.npz 에 담긴 목록을 쓴다")
    ap.add_argument("--all29", action="store_true", help="29관절 전체 그림도 생성")
    ap.add_argument("--curves", choices=["all", "target", "state"], default="all",
                    help="all=3곡선(회색은 옅은 참고선), target=파랑+빨강, state=회색+빨강. "
                         "빨강(모델)은 평가 대상이라 어느 경우에도 남는다")
    ap.add_argument("--ylim", choices=["limit", "auto"], default="limit",
                    help="limit=관절 물리 가동한계 고정(기본), auto=데이터에 맞춰 자동")
    cfg = ap.parse_args()

    plt.rcParams["font.family"] = ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    d = np.load(cfg.decoded)

    # 에피소드로 묶고 파일명 안에 체크포인트를 zero-pad 로 넣는다.
    # 이름순 정렬만으로 "같은 에피소드를 체크포인트끼리" 비교하게 된다.
    arm_dir = cfg.out / "rightarm_only"
    all_dir = cfg.out / "all29"
    arm_dir.mkdir(parents=True, exist_ok=True)
    if cfg.all29:
        all_dir.mkdir(parents=True, exist_ok=True)

    # 에피소드 번호·체크포인트 목록은 decoded.npz 에 담겨 있다(구버전 파일은 없을 수
    # 있어 val5 기본값으로 되돌린다).
    ep_index = (d["ep_index"].tolist() if "ep_index" in d.files
                else [49, 50, 51, 52, 53])
    checkpoints = cfg.checkpoints or (d["checkpoints"].tolist() if "checkpoints" in d.files
                                      else [2000, 4000, 6000, 8000, 10000, 20000])

    neg = sorted(e for e in ep_index if e in NEGATIVE_EPISODES)
    print(f"에피소드 {len(ep_index)}개: {ep_index}")
    print(f"음성(NEG): {neg if neg else '없음'}  "
          f"(wbc_decoder.NEGATIVE_EPISODES 목록 기준, 자동판정 아님)")
    print(f"체크포인트 {len(checkpoints)}개: {checkpoints}")

    for ck in checkpoints:
        for k, ep in enumerate(ep_index):
            # 파일명에 POS/NEG 를 박아 그림 목록만 봐도 음성 에피소드를 알 수 있게 한다.
            name = f"ep{ep:03d}_{episode_kind(ep)}_ck{ck:05d}.png"
            plot_set(d, ck, k, ep, RIGHT_ARM, cfg.fps, arm_dir / name, "right arm 7 joints",
                     cfg.curves, cfg.ylim)
            if cfg.all29:
                plot_set(d, ck, k, ep, list(range(29)), cfg.fps, all_dir / name,
                         "all 29 joints", cfg.curves, cfg.ylim)
        print(f"  ck{ck} 완료")
    print(f"→ {arm_dir}" + (f", {all_dir}" if cfg.all29 else ""))


if __name__ == "__main__":
    main()
