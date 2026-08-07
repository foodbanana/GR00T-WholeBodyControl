# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Save open-loop predictions to .npz so analysis can be redone without a GPU.

``gr00t/eval/open_loop_eval.py`` renders one 800x25600 px jpeg per trajectory and
throws the predictions away. Two things become impossible as a result:

* **Comparing plots.** Matplotlib autoscales every subplot independently, so the
  same absolute error looks tiny on a high-amplitude dimension and enormous on a
  quiet one. Judging two checkpoints — or two trajectories — by eye is unsound
  unless the axes are shared, and the axes cannot be changed after the fact.
* **Asking anything else.** Per-dimension error rankings, or "how much would
  snapping predictions to SONIC's 1/16 grid change the numbers", need the arrays.

``dump`` runs the same rollout as ``open_loop_eval`` and writes the arrays.
``report`` then works entirely offline.

Example:

    # once per checkpoint (needs a GPU)
    uv run python scripts/eval/dump_open_loop_predictions.py dump \\
        --model-path .../checkpoint-2000 \\
        --dataset-path .../raise_arm_banana_val5 \\
        --embodiment-tag UNITREE_G1_SONIC \\
        --out-dir outputs/preds/ck2000

    # afterwards, as often as you like (no GPU)
    uv run python scripts/eval/dump_open_loop_predictions.py report \\
        --pred-dirs outputs/preds/ck2000 outputs/preds/ck20000 \\
        --plot-dir outputs/preds/compare --top-k 6
"""

from copy import deepcopy
import json
from pathlib import Path

from gr00t.data.dataset.lerobot_episode_loader import LeRobotEpisodeLoader
from gr00t.data.dataset.sharded_single_step_dataset import extract_step_data
from gr00t.data.embodiment_tags import EmbodimentTag
from gr00t.data.utils import parse_observation_gr00t
import numpy as np
import tyro


# SONIC reports motion tokens on a 1/16 grid (verified: every value in the
# dataset is an exact multiple of 0.0625). The deploy path does NOT snap --
# `pack_latent_action_message` ships float32 as-is -- so evaluation must not
# snap either. `report` prints the snapped variant purely to show how much a
# hypothetical snap would move the number, i.e. whether it is worth trying on
# the robot at all.
QUANT_STEP = 0.0625


def _rollout(policy, loader, traj_id: int, embodiment_tag, action_keys, steps, execution_horizon):
    """Replicate open_loop_eval's rollout and return (gt, pred) as (T, D) arrays."""
    traj = loader[traj_id]
    actual_steps = min(steps, len(traj))

    obs_modality_configs = deepcopy(loader.modality_configs)
    obs_modality_configs.pop("action")

    preds = []
    for step_count in range(0, actual_steps, execution_horizon):
        data_point = extract_step_data(traj, step_count, obs_modality_configs, embodiment_tag)
        obs = {}
        for k, v in data_point.states.items():
            obs[f"state.{k}"] = v
        for k, v in data_point.images.items():
            obs[f"video.{k}"] = np.array(v)
        for language_key in loader.modality_configs["language"].modality_keys:
            obs[language_key] = data_point.text

        action_chunk, _ = policy.get_action(parse_observation_gr00t(obs, loader.modality_configs))
        action_chunk = {f"action.{k}": action_chunk[k][0] for k in action_chunk}
        for j in range(execution_horizon):
            preds.append(
                np.concatenate(
                    [
                        np.atleast_1d(np.atleast_1d(action_chunk[f"action.{key}"])[j])
                        for key in action_keys
                    ],
                    axis=0,
                )
            )

    gt = np.concatenate(
        [np.vstack([a for a in traj[f"action.{key}"]]) for key in action_keys], axis=-1
    )[:actual_steps]
    pred = np.array(preds)[:actual_steps]
    assert gt.shape == pred.shape, f"gt {gt.shape} != pred {pred.shape}"
    return gt, pred


def dump(
    model_path: str,
    dataset_path: str,
    out_dir: str,
    embodiment_tag: str = "UNITREE_G1_SONIC",
    traj_ids: list[int] | None = None,
    steps: int = 200,
    execution_horizon: int = 16,
    denoising_steps: int = 4,
    modality_keys: list[str] | None = None,
):
    """Run the rollout for each trajectory and save arrays to ``out_dir``.

    Args:
        model_path: Checkpoint directory (standalone, as saved by training).
        dataset_path: LeRobot dataset root, typically the held-out split.
        out_dir: Where to write ``traj_<id>.npz`` and ``meta.json``.
        traj_ids: Trajectories to run. Defaults to every episode in the dataset.
        steps: Cap on steps per trajectory (also capped by episode length).
        execution_horizon: Steps executed per inference; must be <= chunk length.
        denoising_steps: Flow-matching sampling steps, matched to deployment.
        modality_keys: Action keys to record. Defaults to motion_token only,
            since the hand joints are identically zero in this dataset.
    """
    from gr00t.policy.gr00t_policy import Gr00tPolicy
    import torch

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
    action_keys = modality_keys or ["motion_token"]
    ids = traj_ids if traj_ids is not None else list(range(len(loader)))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for traj_id in ids:
        gt, pred = _rollout(policy, loader, traj_id, tag, action_keys, steps, execution_horizon)
        np.savez_compressed(out / f"traj_{traj_id}.npz", gt=gt, pred=pred)
        mse = float(((gt - pred) ** 2).mean())
        summary[str(traj_id)] = mse
        print(f"  traj {traj_id}: shape {gt.shape}  MSE {mse:.8f}")

    (out / "meta.json").write_text(
        json.dumps(
            {
                "model_path": str(model_path),
                "dataset_path": str(dataset_path),
                "embodiment_tag": embodiment_tag,
                "action_keys": action_keys,
                "execution_horizon": execution_horizon,
                "denoising_steps": denoising_steps,
                "traj_ids": ids,
                "mse": summary,
            },
            indent=2,
        )
    )
    print(f"Saved {len(ids)} trajectories to {out}")


def _load(pred_dir: Path):
    meta = json.loads((pred_dir / "meta.json").read_text())
    data = {int(p.stem.split("_")[1]): np.load(p) for p in sorted(pred_dir.glob("traj_*.npz"))}
    return meta, data


def report(
    pred_dirs: list[str],
    plot_dir: str | None = None,
    top_k: int = 6,
    dims: list[int] | None = None,
    reference_traj: int | None = None,
):
    """Compare dumped runs: per-trajectory and per-dimension errors, plus plots.

    Plots share one y-axis per dimension across every run, so the same absolute
    error occupies the same vertical space everywhere -- which is exactly what
    the autoscaled jpegs from ``open_loop_eval`` cannot give you.

    Args:
        pred_dirs: One or more directories produced by ``dump``.
        plot_dir: Where to write per-dimension comparison figures. Skipped if None.
        top_k: How many dimensions to plot, ranked by ground-truth motion.
        dims: Explicit dimension list, overriding ``top_k``.
        reference_traj: Trajectory whose ground-truth motion ranks the dimensions.
            Defaults to the trajectory that moves most (i.e. a positive example).
    """
    runs = {Path(d).name: _load(Path(d)) for d in pred_dirs}
    traj_ids = sorted(set.intersection(*[set(data) for _, data in runs.values()]))
    print(f"runs: {list(runs)}\ntrajectories: {traj_ids}\n")

    print("=== per-trajectory MSE ===")
    header = f"{'traj':>6} " + "".join(f"{name:>16}" for name in runs)
    print(header)
    for t in traj_ids:
        row = f"{t:>6} "
        for _, data in runs.values():
            d = data[t]
            row += f"{((d['gt'] - d['pred']) ** 2).mean():>16.8f}"
        print(row)

    # Rank trajectories by how much the ground truth actually moves, so
    # near-static (negative) episodes are not averaged in with the rest.
    first = next(iter(runs.values()))[1]
    motion = {t: float(first[t]["gt"].std(axis=0).mean()) for t in traj_ids}
    active = [t for t in traj_ids if motion[t] > 0.5 * max(motion.values())]
    quiet = [t for t in traj_ids if t not in active]
    print(f"\nmoving trajectories {active}, near-static {quiet}")

    print("\n=== mean MSE over moving trajectories (the ranking that matters) ===")
    for name, (_, data) in runs.items():
        vals = [((data[t]["gt"] - data[t]["pred"]) ** 2).mean() for t in active]
        snapped = [
            ((data[t]["gt"] - np.round(data[t]["pred"] / QUANT_STEP) * QUANT_STEP) ** 2).mean()
            for t in active
        ]
        print(
            f"  {name:>16}  continuous {np.mean(vals):.8f}   "
            f"snapped-to-1/16 {np.mean(snapped):.8f}  "
            f"({100 * (np.mean(snapped) / np.mean(vals) - 1):+.1f}%)"
        )
    print("  (snapped is diagnostic only -- the deploy path sends float32 unrounded)")

    ref = reference_traj if reference_traj is not None else max(motion, key=motion.get)
    gt_ref = first[ref]["gt"]
    plot_dims = dims or list(np.argsort(gt_ref.std(axis=0))[::-1][:top_k])
    print(f"\n=== per-dimension MSE on traj {ref} (dims ranked by GT motion) ===")
    print(f"{'dim':>5} {'GT std':>9} " + "".join(f"{n:>16}" for n in runs))
    for d in plot_dims:
        row = f"{int(d):>5} {gt_ref[:, d].std():>9.4f} "
        for _, data in runs.values():
            e = data[ref]["gt"][:, d] - data[ref]["pred"][:, d]
            row += f"{(e**2).mean():>16.8f}"
        print(row)

    if plot_dir is None:
        return

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = Path(plot_dir)
    out.mkdir(parents=True, exist_ok=True)
    for t in traj_ids:
        for d in plot_dims:
            d = int(d)
            # One shared y-range per dimension: the ground truth's range on the
            # reference (moving) trajectory, padded. Errors are then read
            # against the size of the real motion, and every run and every
            # trajectory is drawn on the same scale.
            lo, hi = gt_ref[:, d].min(), gt_ref[:, d].max()
            pad = 0.15 * max(hi - lo, 1e-6)
            fig, ax = plt.subplots(figsize=(11, 4))
            ax.plot(runs[list(runs)[0]][1][t]["gt"][:, d], "k-", lw=2, label="gt")
            for name, (_, data) in runs.items():
                ax.plot(data[t]["pred"][:, d], lw=1.2, alpha=0.85, label=name)
            ax.set_ylim(lo - pad, hi + pad)
            ax.set_title(f"traj {t} — action dim {d}  (y-range from traj {ref} ground truth)")
            ax.set_xlabel("timestep (÷25 = seconds)")
            ax.grid(alpha=0.3)
            ax.legend(fontsize=8)
            plt.tight_layout()
            plt.savefig(out / f"traj{t}_dim{d}.png", dpi=110)
            plt.close()
    print(f"\nWrote {len(traj_ids) * len(plot_dims)} figures to {out}")


if __name__ == "__main__":
    tyro.extras.subcommand_cli_from_dict({"dump": dump, "report": report})
