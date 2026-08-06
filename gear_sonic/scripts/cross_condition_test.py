#!/usr/bin/env python3
"""교차 조건 테스트 — VLA가 이미지를 보고 "바나나 유무"를 판별하는가.

로봇 상태(state)는 고정하고 **카메라 이미지만** 바꿔치기해서 예측 토큰이
갈라지는지 본다. closed-loop 환경 구축 없이 조건부 판별 능력만 분리해 측정한다.

세 조건을 비교한다 (기준 프레임의 state 는 항상 동일):

  self    : 자기 자신의 이미지              → 기준선
  within  : 같은 클래스 다른 에피소드 이미지 → **잡음 바닥값**
  cross   : 반대 클래스 에피소드 이미지      → **신호**

cross >> within 이면 모델이 이미지를 보고 판별하는 것이고,
cross ≈ within 이면 이미지를 사실상 무시하고 있다는 뜻이다.

질의 프레임은 팔이 아직 올라가기 전(초반)으로 잡는다. 팔이 이미 올라간 뒤의
이미지에는 팔 자체가 찍혀 있어 "바나나를 봤는가"와 교란되기 때문이다.
"""

from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import tyro

from gr00t.data.dataset.lerobot_episode_loader import LeRobotEpisodeLoader
from gr00t.data.dataset.sharded_single_step_dataset import extract_step_data
from gr00t.data.embodiment_tags import EmbodimentTag
from gr00t.data.utils import parse_observation_gr00t


# 음성 에피소드는 명시 지정이다(가동범위 자동판정 아님).
# `gear_sonic/scripts/wbc_decoder.py` 의 NEGATIVE_EPISODES 와 같은 값을 유지할 것.
#   4, 9, 49 : v1 수집분
#   55~78    : 2026-08-06 추가 수집분 (빈 공간 → 정지)
NEGATIVE_EPISODES = {4, 9, 49} | set(range(55, 79))


def load_episode_index(dataset_path: str) -> list[int]:
    """데이터셋의 traj_id 순서대로 원본 episode_index 를 읽는다."""
    import json
    from pathlib import Path as _P
    meta = _P(dataset_path) / "meta" / "episodes.jsonl"
    return [json.loads(x)["episode_index"] for x in meta.read_text().splitlines() if x.strip()]


def main(
    model_path: str,
    dataset_path: str,
    out_dir: str,
    embodiment_tag: str = "UNITREE_G1_SONIC",
    frames: tuple[int, ...] = (10, 15, 20, 25, 30, 35, 40),
    denoising_steps: int = 4,
    max_bases: int = 4,
    max_sources: int = 6,
) -> None:
    """max_bases / max_sources 로 조합 수를 제한한다.

    val이 14개로 늘면 14×14×7 = 1372 조합이 되어 과하다. 양성·음성에서 고르게
    앞쪽 몇 개만 골라 쓴다.
    """
    from gr00t.policy.gr00t_policy import Gr00tPolicy
    import torch

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    tag = EmbodimentTag.resolve(embodiment_tag)
    policy = Gr00tPolicy(
        embodiment_tag=tag,
        model_path=model_path,
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    policy.model.action_head.num_inference_timesteps = denoising_steps
    loader = LeRobotEpisodeLoader(
        dataset_path=dataset_path, modality_configs=policy.get_modality_config()
    )

    obs_cfg = deepcopy(loader.modality_configs)
    obs_cfg.pop("action")

    ep_index = load_episode_index(dataset_path)
    kind = ["NEG" if e in NEGATIVE_EPISODES else "POS" for e in ep_index]
    trajs = [loader[i] for i in range(len(ep_index))]
    print("에피소드:", {ep_index[i]: (kind[i], len(trajs[i])) for i in range(len(trajs))})

    def pick(n, want_kind):
        return [i for i in range(len(ep_index)) if kind[i] == want_kind][:n]

    bases = sorted(pick(max_bases // 2, "NEG") + pick(max_bases - max_bases // 2, "POS"))
    sources = sorted(pick(max_sources // 2, "NEG") + pick(max_sources - max_sources // 2, "POS"))
    print(f"base(state) 로 쓸 traj: {[ep_index[i] for i in bases]}")
    print(f"이미지 출처로 쓸 traj : {[ep_index[i] for i in sources]}")

    def data_point(i, t):
        return extract_step_data(trajs[i], t, obs_cfg, tag)

    records = []
    for base in bases:
        for t in frames:
            if t >= len(trajs[base]):
                continue
            dp = data_point(base, t)
            obs_base = {f"state.{k}": v for k, v in dp.states.items()}
            for k, v in dp.images.items():
                obs_base[f"video.{k}"] = np.array(v)
            for lk in loader.modality_configs["language"].modality_keys:
                obs_base[lk] = dp.text
            img_key = next(k for k in obs_base if k.startswith("video."))

            for src in sorted(set(sources) | {base}):
                if t >= len(trajs[src]):
                    continue
                obs = dict(obs_base)
                if src != base:
                    dsrc = data_point(src, t)
                    obs[img_key] = np.array(next(iter(dsrc.images.values())))

                chunk, _ = policy.get_action(
                    parse_observation_gr00t(obs, loader.modality_configs)
                )
                token = np.asarray(chunk["motion_token"][0])  # (horizon, 64)

                cond = ("self" if src == base
                        else "within" if kind[src] == kind[base]
                        else "cross")
                records.append(dict(base=base, src=src, frame=int(t), cond=cond,
                                    base_ep=ep_index[base], src_ep=ep_index[src],
                                    base_kind=kind[base], src_kind=kind[src]))
                np.save(out / f"tok_b{base}_s{src}_t{t}.npy", token.astype(np.float32))
            print(f"  base ep{ep_index[base]} frame {t} 완료")

    (out / "records.json").write_text(json.dumps(records, indent=1))
    (out / "meta.json").write_text(json.dumps(
        {"model_path": model_path, "dataset_path": dataset_path,
         "frames": list(frames), "denoising_steps": denoising_steps,
         "traj_episode": ep_index, "traj_kind": kind,
         "bases": bases, "sources": sources}, indent=2))
    print(f"저장 완료: {out}  ({len(records)} 조합)")


if __name__ == "__main__":
    tyro.cli(main)
