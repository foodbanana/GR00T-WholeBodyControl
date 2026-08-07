#!/usr/bin/env python3
"""체크포인트 스윕 곡선 — decoded joint 오차 vs val/loss 를 한 장에 겹쳐 본다.

`eval_decoded_openloop.py` 가 만든 `metrics.csv` 를 읽어 체크포인트별 추이를 그린다.
val/loss 는 학습 로그에서 뽑은 `step<TAB>value` 형식 파일로 넘긴다(선택).

목적: **어떤 체크포인트를 실기에 쓸지** 고르는 것. v1에서 val/loss(잠재공간)와
decoded joint(관절공간) 순위가 어긋난 전례가 있어 두 곡선을 나란히 본다.

사용:
    scratchpad/onnxenv/bin/python gear_sonic/scripts/plot_checkpoint_sweep.py \\
        --metrics eval_results/decoded_openloop_v2_h10/metrics.csv \\
        --valloss eval_results/decoded_openloop_v2_h10/valloss.tsv \\
        --out eval_results/decoded_openloop_v2_h10
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

# DejaVu Sans 에는 한글 글리프가 없어 라벨이 네모로 깨진다. Noto Sans CJK 로 지정.
plt.rcParams["font.family"] = ["Noto Sans CJK KR", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

# Phase 0 에서 실측한 25Hz→50Hz 보간의 측정 바닥값(오른팔). 이보다 낮은 오차는
# 모델 성능이 아니라 측정 한계다.
FLOOR_DEG = 1.53


def rms_deg(vals) -> float:
    """rad^2 MSE 목록을 도 단위 RMSE 로 합친다 (에피소드 평균은 MSE 에서 취한다)."""
    return float(np.degrees(np.sqrt(np.mean(vals))))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics", type=Path, required=True)
    ap.add_argument("--valloss", type=Path, default=None,
                    help="'step<TAB>val/loss' 형식 파일 (선택)")
    ap.add_argument("--out", type=Path, required=True)
    cfg = ap.parse_args()

    rows = list(csv.DictReader(open(cfg.metrics)))
    cks = sorted({int(r["checkpoint"]) for r in rows})
    pos: dict[int, list] = defaultdict(list)
    neg: dict[int, list] = defaultdict(list)
    allj: dict[int, list] = defaultdict(list)
    per_ep: dict[int, dict[int, float]] = defaultdict(dict)
    for r in rows:
        ck = int(r["checkpoint"])
        (neg if r["kind"] == "negative" else pos)[ck].append(float(r["mse_rightarm_rad2"]))
        if r["kind"] == "positive":
            allj[ck].append(float(r["mse_all_rad2"]))
        per_ep[int(r["episode"])][ck] = float(r["rmse_rightarm_deg"])

    p = np.array([rms_deg(pos[c]) for c in cks])
    n = np.array([rms_deg(neg[c]) for c in cks])
    a = np.array([rms_deg(allj[c]) for c in cks])

    vl = None
    if cfg.valloss and cfg.valloss.exists():
        raw = {}
        for line in open(cfg.valloss):
            parts = line.split()
            if len(parts) >= 2:
                raw[int(parts[0])] = float(parts[1])
        # 덤프한 체크포인트에 해당하는 step 만 뽑는다.
        vl = np.array([raw.get(c, np.nan) for c in cks])

    cfg.out.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(3, 1, figsize=(11, 13), sharex=True)

    # (1) 양성/음성 요약 + val/loss 오버레이
    ax = axes[0]
    ax.plot(cks, p, "o-", color="tab:red", lw=2, label=f"양성 {len(pos[cks[0]])}ep 오른팔 RMSE")
    ax.plot(cks, n, "s-", color="tab:orange", lw=2, label=f"음성 {len(neg[cks[0]])}ep 오른팔 RMSE")
    ax.plot(cks, a, "^--", color="tab:purple", lw=1.5, alpha=.7, label="양성 전체29관절 RMSE")
    ax.axhline(FLOOR_DEG, color="gray", ls=":", lw=1.5)
    ax.text(cks[-1], FLOOR_DEG, f" 측정하한 {FLOOR_DEG}°", va="bottom", ha="right",
            color="gray", fontsize=9)
    best = int(np.argmin(p))
    ax.axvline(cks[best], color="tab:red", ls="--", alpha=.4)
    ax.annotate(f"decoded 최적\nck{cks[best]}  {p[best]:.2f}°",
                (cks[best], p[best]), textcoords="offset points", xytext=(8, 14),
                color="tab:red", fontsize=10, fontweight="bold")
    ax.set_ylabel("오른팔 RMSE (도)")
    ax.set_title("체크포인트 스윕 — 관절공간 오차", fontsize=13, fontweight="bold")
    ax.grid(alpha=.3)
    ax.legend(loc="upper left", fontsize=9)
    if vl is not None and np.isfinite(vl).any():
        ax2 = ax.twinx()
        ax2.plot(cks, vl, "d-", color="tab:blue", lw=1.5, alpha=.6, label="val/loss (잠재공간)")
        bv = int(np.nanargmin(vl))
        ax2.axvline(cks[bv], color="tab:blue", ls="--", alpha=.4)
        ax2.annotate(f"val/loss 최적\nck{cks[bv]}  {vl[bv]:.4f}",
                     (cks[bv], vl[bv]), textcoords="offset points", xytext=(8, -28),
                     color="tab:blue", fontsize=10, fontweight="bold")
        ax2.set_ylabel("val/loss (motion_token)", color="tab:blue")
        ax2.tick_params(axis="y", labelcolor="tab:blue")
        ax2.legend(loc="upper right", fontsize=9)

    # (2) 양성 에피소드별 — 순위가 특정 에피소드에 끌려다니는지 본다
    ax = axes[1]
    for ep in sorted(per_ep):
        if ep in _neg_eps(rows):
            continue
        ax.plot(cks, [per_ep[ep][c] for c in cks], "o-", lw=1.2, ms=4, alpha=.8, label=f"ep{ep}")
    ax.plot(cks, p, "k-", lw=2.5, label="평균")
    ax.set_ylabel("오른팔 RMSE (도)")
    ax.set_title("양성 에피소드별 — 편차가 크면 순위를 믿을 수 없다", fontsize=12)
    ax.grid(alpha=.3)
    ax.legend(ncol=5, fontsize=8)

    # (3) 음성 에피소드별
    ax = axes[2]
    for ep in sorted(_neg_eps(rows)):
        ax.plot(cks, [per_ep[ep][c] for c in cks], "o-", lw=1.2, ms=4, alpha=.8, label=f"ep{ep}")
    ax.plot(cks, n, "k-", lw=2.5, label="평균")
    ax.axhline(FLOOR_DEG, color="gray", ls=":", lw=1.5)
    ax.set_xlabel("checkpoint (step)")
    ax.set_ylabel("오른팔 RMSE (도)")
    ax.set_title("음성 에피소드별 — 측정하한에 붙어 있으면 팔이 정지한 것", fontsize=12)
    ax.grid(alpha=.3)
    ax.legend(ncol=7, fontsize=8)

    fig.tight_layout()
    out = cfg.out / "checkpoint_sweep.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)

    print(f"{'ckpt':>7} {'양성':>8} {'음성':>8} {'전체29':>8}" + ("  val/loss" if vl is not None else ""))
    for i, c in enumerate(cks):
        line = f"{c:>7} {p[i]:>8.2f} {n[i]:>8.2f} {a[i]:>8.2f}"
        if vl is not None:
            line += f"  {vl[i]:.4f}" if np.isfinite(vl[i]) else "       -"
        print(line)
    print(f"\ndecoded joint 최적: ck{cks[best]}  {p[best]:.2f}°")
    if vl is not None and np.isfinite(vl).any():
        bv = int(np.nanargmin(vl))
        m = np.isfinite(vl)
        r = np.corrcoef(vl[m], p[m])[0, 1]
        print(f"val/loss 최적    : ck{cks[bv]}  {vl[bv]:.4f}")
        print(f"두 지표 상관     : {r:+.3f}  (1에 가까울수록 val/loss 로 골라도 된다는 뜻)")
    print(f"저장: {out}")


def _neg_eps(rows) -> set:
    return {int(r["episode"]) for r in rows if r["kind"] == "negative"}


if __name__ == "__main__":
    main()
