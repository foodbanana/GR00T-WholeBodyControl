#!/usr/bin/env python3
"""LeRobot v2.1 데이터셋을 에피소드 단위로 train/val 로 분할한다.

`process_dataset.py` 가 여러 수집본을 하나로 병합한 뒤, 그 병합본을 학습/검증으로
나누는 용도다. 원본 `episode_index` 와 파일명을 **그대로 보존**하므로 분할 결과가
어느 원본 에피소드인지 항상 추적 가능하다.

사용 예 (KIST 서버 raise-arm-banana-val-20260801 학습과 동일한 분할):

    python3 gear_sonic/scripts/split_dataset.py \\
        --source     outputs/raise_arm_banana_merged \\
        --out-train  outputs/raise_arm_banana_train50 \\
        --out-val    outputs/raise_arm_banana_val5 \\
        --val-episodes 49 50 51 52 53

`meta/stats.json` 과 `meta/relative_stats.json` 은 만들지 않는다 — 학습 스크립트가
데이터셋을 처음 로드할 때 자동 생성한다(서버 산출물도 그렇게 만들어졌다).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def video_keys(info: dict) -> list[str]:
    return [k for k, v in info.get("features", {}).items() if v.get("dtype") == "video"]


def write_split(
    source: Path, out: Path, episodes: list[dict], info: dict, overwrite: bool
) -> None:
    if out.exists():
        if not overwrite:
            sys.exit(f"✗ 이미 존재한다: {out}  (--overwrite 를 주면 지우고 다시 만든다)")
        shutil.rmtree(out)

    keys = video_keys(info)
    (out / "meta").mkdir(parents=True)
    (out / "data" / "chunk-000").mkdir(parents=True)
    for k in keys:
        (out / "videos" / "chunk-000" / k).mkdir(parents=True)

    data_tmpl = info["data_path"]
    video_tmpl = info["video_path"]

    for ep in episodes:
        idx = ep["episode_index"]
        rel = data_tmpl.format(episode_chunk=0, episode_index=idx)
        src = source / rel
        if not src.exists():
            sys.exit(f"✗ parquet 없음: {src}")
        shutil.copy2(src, out / rel)

        for k in keys:
            rel_v = video_tmpl.format(episode_chunk=0, video_key=k, episode_index=idx)
            src_v = source / rel_v
            if not src_v.exists():
                sys.exit(f"✗ mp4 없음: {src_v}")
            shutil.copy2(src_v, out / rel_v)

    n_ep = len(episodes)
    n_frames = sum(e["length"] for e in episodes)

    new_info = dict(info)
    new_info["total_episodes"] = n_ep
    new_info["total_frames"] = n_frames
    new_info["total_videos"] = n_ep * max(len(keys), 1)
    new_info["total_chunks"] = 1
    # splits 는 "개수" 기준이다. episode_index 가 연속이 아니어도(예: 0..48 + 54)
    # 0:<개수> 로 쓴다 — 서버 train50/val5 산출물과 동일한 규칙.
    new_info["splits"] = {"train": f"0:{n_ep}"}

    (out / "meta" / "info.json").write_text(
        json.dumps(new_info, indent=4, ensure_ascii=False) + "\n"
    )
    write_jsonl(out / "meta" / "episodes.jsonl", episodes)
    for name in ("modality.json", "tasks.jsonl"):
        src_m = source / "meta" / name
        if src_m.exists():
            shutil.copy2(src_m, out / "meta" / name)
        else:
            print(f"  ! 경고: {name} 이 원본에 없다")

    print(f"  {out.name}: {n_ep} ep / {n_frames} frame / {n_ep * len(keys)} mp4")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, required=True, help="병합된 원본 데이터셋")
    ap.add_argument("--out-train", type=Path, required=True)
    ap.add_argument("--out-val", type=Path, required=True)
    ap.add_argument(
        "--val-episodes",
        type=int,
        nargs="+",
        required=True,
        help="val 로 뺄 원본 episode_index 목록 (나머지가 train)",
    )
    ap.add_argument("--overwrite", action="store_true")
    cfg = ap.parse_args()

    info = json.loads((cfg.source / "meta" / "info.json").read_text())
    all_eps = load_jsonl(cfg.source / "meta" / "episodes.jsonl")
    by_idx = {e["episode_index"]: e for e in all_eps}

    missing = [i for i in cfg.val_episodes if i not in by_idx]
    if missing:
        sys.exit(f"✗ 원본에 없는 episode_index: {missing}")
    if len(set(cfg.val_episodes)) != len(cfg.val_episodes):
        sys.exit("✗ --val-episodes 에 중복이 있다")

    val_set = set(cfg.val_episodes)
    val_eps = [e for e in all_eps if e["episode_index"] in val_set]
    train_eps = [e for e in all_eps if e["episode_index"] not in val_set]

    print(f"원본 {cfg.source}: {len(all_eps)} ep / {sum(e['length'] for e in all_eps)} frame")
    write_split(cfg.source, cfg.out_train, train_eps, info, cfg.overwrite)
    write_split(cfg.source, cfg.out_val, val_eps, info, cfg.overwrite)

    assert len(train_eps) + len(val_eps) == len(all_eps)
    print("완료 — train ∪ val == 원본, 겹침 없음")


if __name__ == "__main__":
    main()
