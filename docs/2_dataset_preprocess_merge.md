# 2. 데이터셋 전처리 & merge

수집한 세션들(`outputs/<날짜>/`)을 정제하고 하나로 병합해, **파인튜닝에 바로 넣을 수 있는
LeRobot v2.1 데이터셋**을 만든다.

```
+-----------------+     +-----------------+     +-----------------+     +-----------------+
| 1. Collect      |     | 2. Preprocess   |     | 3. Fine-tune    |     | 4. Deploy       |
| VR teleop +     | --> | clean / merge   | --> | Isaac-GR00T     | --> | PolicyServer +  |
| data export     |     | verify / split  |     | N1.7            |     | SONIC           |
+-----------------+     +-----------------+     +-----------------+     +-----------------+
       ↑ 1번 문서            ↑ 이 문서              ↑ 3번 문서              ↑ 4·5번 문서
```

**이전** ← [1. 데이터 수집](1_data_collection.md) · **다음** → [3. GR00T N1.7 파인튜닝](3_finetune_groot_n17.md)

---

## 0. 전체 흐름

```
outputs/2026-07-22-11-00-51/  ─┐
outputs/2026-07-22-11-09-30/  ─┼─ ① 세션별 verify ─→ ② process_dataset.py ─→ ③ 정제본 verify
outputs/2026-07-22-13-23-08/  ─┘   (오염 에피소드      (clean + merge)          (VLA-facing 키)
                                    골라내기)                │
                                                             ▼
                                              outputs/raise_arm_banana_merged
                                                             │
                                              ④ split_dataset.py (train / val)
                                                             │
                                              ⑤ rsync → GPU 서버 (3번 문서)
```

모든 명령은 **DGX Spark** 에서 `.venv_data_collection` 으로 실행한다.

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
```

| 스크립트 | 역할 |
|---|---|
| [gear_sonic/scripts/verify_dataset.py](../gear_sonic/scripts/verify_dataset.py) | 6개 항목 검증. 종료코드 0/1 |
| [gear_sonic/scripts/process_dataset.py](../gear_sonic/scripts/process_dataset.py) | 폐기 에피소드 제거 + stale SMPL 프레임 제거 + **다중 세션 merge** |
| [gear_sonic/scripts/split_dataset.py](../gear_sonic/scripts/split_dataset.py) | 에피소드 단위 train/val 분할 (`episode_index` 보존) |

---

## ★ STEP 0 — teleop 모드에 따라 옵션이 갈린다 (가장 중요)

`process_dataset.py` 의 stale SMPL 정제는 **`teleop.smpl_pose` 컬럼이 유효할 때만** 의미가 있다.
어떤 모드로 수집했는지에 따라 반드시 갈라 쓴다.

| 수집 모드 | 명령 | 결과 |
|---|---|---|
| **VR whole-body teleop** (SMPL 모드) | 그냥 실행 (기본값 `--remove-stale-smpl`) | SMPL 스트림이 정상이므로 all-zero/frozen 프레임은 진짜 드롭이다 |
| **VR 3-point teleop** (`PLANNER_VR_3PT`, `stream_mode=5`) | **`--no-remove-stale-smpl` 필수** | 안 주면 **프레임이 100% 삭제된다** |

> ### ⛔ 3-point 모드에서 `--no-remove-stale-smpl` 을 빼면 데이터가 전부 사라진다
>
> 3-point 모드는 전신 SMPL 을 아예 발행하지 않아 **`teleop.smpl_pose` 가 항상 all-zero** 다.
> `build_stale_mask()` 는 all-zero 행을 "드롭 프레임"으로 판정하므로 **모든 행이 제거 대상**이 된다.
>
> **실제 텔레오퍼레이션 신호는 다른 컬럼에 들어 있다** — `teleop.vr_3pt_position` 과 `action.wbc`.
> 즉 `teleop.smpl_pose` 가 비어 있어도 데이터는 멀쩡하다.
>
> **v1 `raise_arm_banana_merged` 는 3-point 수집분이라 이 플래그를 넣고 병합했다.**

어떤 모드로 찍었는지 기억이 안 나면, 병합 전에 한 세션만 확인한다:

```bash
python3 -c "
import pandas as pd, glob, numpy as np
f = sorted(glob.glob('outputs/<세션>/data/chunk-000/*.parquet'))[0]
df = pd.read_parquet(f)
print('컬럼:', [c for c in df.columns if c.startswith('teleop')])
if 'teleop.smpl_pose' in df:
    a = np.stack(df['teleop.smpl_pose'].values)
    print('smpl_pose all-zero 비율: %.1f%%' % (100*np.all(a==0,axis=1).mean()))
"
```

**all-zero 비율이 100% 면 3-point 수집분**이다 → `--no-remove-stale-smpl` 을 반드시 넣는다.

### 무엇을 지우는가

**stale SMPL 마스크** ([process_dataset.py:65](../gear_sonic/scripts/process_dataset.py#L65)):

- `teleop.smpl_pose` 가 **all-zero 인 행** → 제거
- 그 앞에 **연속으로 얼어붙어 있던(직전과 동일) 행들** → 제거
- **zero 행으로 이어지지 않는 frozen 구간은 건드리지 않는다** — SMPL 스트림이 수집 루프보다
  약간 느릴 때 자연히 생기는 것이라 정상 데이터다

**폐기 에피소드** (`--remove-discarded`, 기본 켜짐): 수집 중 PICO **왼쪽 grip + B** 로 버린
에피소드가 `meta/info.json` 의 `discarded_episode_indices` 에 남아 있고, 이 목록대로 제거한다.
처리 후 그 키는 출력에서 삭제된다.

---

## STEP 1 — 세션별 검증 (오염 에피소드 골라내기)

merge 하기 **전에** 세션마다 돌린다. 오염된 에피소드를 병합본에 끌고 들어가면 나중에 빼기 어렵다.

```bash
python gear_sonic/scripts/verify_dataset.py outputs/2026-07-22-11-00-51
python gear_sonic/scripts/verify_dataset.py outputs/2026-07-22-11-09-30
python gear_sonic/scripts/verify_dataset.py outputs/2026-07-22-13-23-08
```

**검사 4번(연속 정지 1초 이상)** 이 걸리면 D405 wedge 오염이다 → **그 에피소드는 폐기**한다.
검사 항목 전체는 [1번 문서 STEP 4](1_data_collection.md#step-4--데이터셋-검증-dgx-spark) 참조.

옵션: `--no-load`(LeRobot 로딩 스킵, 빠름) / `--no-freeze`(정지 프레임 검사 스킵, 긴 에피소드용)

---

## STEP 2 — 단일 세션 정제 (선택)

> **v1 병합에서는 이 단계를 따로 돌리지 않았다** — [STEP 3](#step-3--여러-세션-merge) 에서
> clean 과 merge 를 한 번에 처리했다. 한 세션만 손볼 때나, 정제 결과를 먼저 확인하고 싶을 때 쓴다.

```bash
# 비파괴 (권장) — 새 폴더로 출력
python gear_sonic/scripts/process_dataset.py \
    --dataset-path outputs/2026-07-22-11-09-30 \
    --output-path outputs/2026-07-22-11-09-30_cleaned

# 결과 확인
python gear_sonic/scripts/verify_dataset.py outputs/2026-07-22-11-09-30_cleaned
```

`--output-path` 를 생략하면 **제자리에서 덮어쓴다**. 원본을 남기고 싶으면 반드시 지정할 것.

**VR 3-point 수집분이면:**

```bash
python gear_sonic/scripts/process_dataset.py \
    --dataset-path outputs/2026-07-22-11-09-30 \
    --output-path outputs/2026-07-22-11-09-30_cleaned \
    --no-remove-stale-smpl
```

---

## STEP 3 — 여러 세션 merge

`process_dataset.py` 는 clean 과 merge 를 같은 명령으로 처리한다. **입력 순서가 곧
`episode_index` 순서**이므로 시간순으로 나열하는 것이 추적에 유리하다.

**v1 `raise_arm_banana_merged`(55 ep) 를 만든 실제 명령** — 세션 10개를 한 번에 병합했다.
STEP 2 를 따로 돌리지 않고 clean 과 merge 를 한 번에 처리했다:

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate

python gear_sonic/scripts/process_dataset.py \
    --dataset-path outputs/raise_arm_banana_01 outputs/raise_arm_banana_02 \
                   outputs/raise_arm_banana_03 outputs/raise_arm_banana_04 \
                   outputs/raise_arm_banana_05 outputs/raise_arm_banana_06 \
                   outputs/raise_arm_banana_07 outputs/raise_arm_banana_08 \
                   outputs/raise_arm_banana_09 outputs/raise_arm_banana_10 \
    --output-path outputs/raise_arm_banana_merged \
    --no-remove-stale-smpl
```

- `--output-path` 에는 원하는 폴더 이름을 쓴다
- **`--no-remove-stale-smpl` 이 여기 붙은 이유는 [STEP 0](#-step-0--teleop-모드에-따라-옵션이-갈린다-가장-중요)** —
  3-point 모드 수집분이라 빼면 프레임이 전부 삭제된다
- 세션 폴더는 수집 시각 자동 이름(`2026-07-22-11-09-30`) 대신 `--dataset-name` 으로
  `raise_arm_banana_01` 처럼 붙여두면 이렇게 나열하기 쉽다

### v2 `raise_arm_banana_merged_v2`(79 ep) — v1 병합본에 음성 2세션을 얹었다

v2 는 원본 10세션부터 다시 병합한 것이 **아니다.** 이미 만들어 둔 v1 병합본에
2026-08-06 에 추가 수집한 **음성 전용 2세션**을 이어 붙였다:

```bash
python gear_sonic/scripts/process_dataset.py \
    --dataset-path outputs/raise_arm_banana_merged \
                   outputs/2026-08-06-20-55-58 \
                   outputs/2026-08-06-21-05-31 \
    --output-path outputs/raise_arm_banana_merged_v2 \
    --no-remove-stale-smpl
```

| 입력 | ep | frame | 내용 |
|---|---|---|---|
| `raise_arm_banana_merged` (v1) | 55 | 11,208 | ep0~54 |
| `2026-08-06-20-55-58` | 13 | 2,057 | 신규 음성 → ep55~67 |
| `2026-08-06-21-05-31` | 11 | 1,636 | 신규 음성 → ep68~78 |
| **= `raise_arm_banana_merged_v2`** | **79** | **14,901** | fps 25, `ego_view` 1대 |

**입력 순서가 곧 `episode_index` 순서**라 신규 음성이 정확히 ep55~78 에 놓였고,
그래서 [`NEGATIVE_EPISODES`](../gear_sonic/scripts/wbc_decoder.py) 가 `{4,9,49} ∪ {55..78}` 이다.
**순서를 바꿔 병합하면 이 상수가 통째로 틀어진다.**

세션이 많으면 목록 파일로:

```bash
cat > datasets.txt <<'EOF'
outputs/raise_arm_banana_01
outputs/raise_arm_banana_02
# ... 한 줄에 하나, '#' 주석 가능
EOF

python gear_sonic/scripts/process_dataset.py \
    --dataset-list datasets.txt \
    --output-path outputs/raise_arm_banana_merged \
    --no-remove-stale-smpl
```

### merge 가 자동으로 막아주는 것

**`script_config` 불일치 검사** ([process_dataset.py:200](../gear_sonic/scripts/process_dataset.py#L200)).
`script_config` 는 수집 당시 C++ deploy 가 5557 로 발행한 `robot_config` 다. 로봇 설정이
다른 세션끼리는 병합할 수 없고, 다르면 어느 데이터셋이 어긋나는지 출력하고 종료한다.

```
ERROR: script_config mismatch across datasets.
  Reference: session_a
  Differs:   session_c
```

이게 뜨면 **로봇 설정이 중간에 바뀐 것**이다. 병합을 강행하지 말고 어느 세션이 다른지부터 확인한다.

`meta/modality.json` 은 첫 번째 소스에서 복사된다.

---

## STEP 4 — 정제본 재검증

```bash
python gear_sonic/scripts/verify_dataset.py outputs/raise_arm_banana_merged
```

### ★ [6] 번 검사가 정제본에서는 다르게 동작한다 — 알고 볼 것

정제본에는 `meta/episodes_stats.jsonl` 이 **없다**(`process_dataset.py` 가 쓰지 않는다).
`verify_dataset.py` 는 이 파일의 존재로 두 갈래로 분기한다
([verify_dataset.py:236](../gear_sonic/scripts/verify_dataset.py#L236)):

| 갈래 | 조건 | [6] 이 하는 일 |
|---|---|---|
| **A** | `episodes_stats.jsonl` **없음** (= 정제본) | native `LeRobotDataset` 로딩을 **스킵**. 대신 **VLA-facing 키만 로컬 확인** — `info.json` 의 video_keys(3카메라) + parquet 의 `observation.state`/`action.wbc`. ⚠️ 안내를 출력하지만 **실패로 치지 않는다** |
| **B** | `episodes_stats.jsonl` **있음** (= 수집 원본) | 기존대로 full 로딩 (`ds[0]` 키 확인 + 3카메라 비디오 실제 디코드/검정 검사) |

**왜 이렇게 고쳤나.** native `LeRobotDataset`(v2.1)은 `episodes_stats.jsonl` 이 없으면
로컬 로딩에 실패하고 **HF Hub 로 폴백해 엉뚱한 404**(`RepositoryNotFoundError`)를 던진다.
그래서 정제본은 항상 FAIL 로 찍혔다. 그런데 실제 파인튜닝 소비자인 **Isaac-GR00T 로더는
이 파일을 요구하지 않는다** — NVIDIA 공식 경로 자체가
`collect → process_dataset.py(이 파일 제거) → launch_finetune` 이다.
즉 기존 [6]은 **"잘못된 로더" 경보**였다.

실측 결과:

| 대상 | [6] 결과 | 종료코드 |
|---|---|---|
| `..._cleaned` (정제본, stats 없음) | ⚠️ 스킵 안내 + ✅ VLA-facing 키 확인 | `0` |
| `2026-07-22-11-09-30` (원본, stats 있음) | ✅ full 로딩 + 3카메라 디코드 | `0` |

> **안전장치** — Isaac-GR00T 가 만에 하나 이 파일을 읽는다면 데이터셋 init 에서 즉시
> 크래시하므로 silent 실패는 없다. 확인하려면 서버에서 30초면 된다:
> `grep -rn "episodes_stats\|stats.json" gr00t/data/`

### info.json 정합성 점검

```bash
cd outputs/raise_arm_banana_merged
python3 - <<'EOF'
import json, pathlib
info = json.loads(pathlib.Path("meta/info.json").read_text())
eps  = [json.loads(l) for l in open("meta/episodes.jsonl")]
npq  = len(list(pathlib.Path("data/chunk-000").glob("*.parquet")))
vids = {d.name: len(list(d.glob("*.mp4"))) for d in pathlib.Path("videos/chunk-000").iterdir()}
n = info["total_episodes"]
print("total_episodes:", n, "| episodes.jsonl:", len(eps), "| parquet:", npq)
print("total_videos  :", info["total_videos"], "| mp4:", vids)
print("total_frames  :", info["total_frames"], "| sum(len):", sum(e["length"] for e in eps))
print("splits        :", info["splits"], "  <- 반드시 0:%d" % n)
print("fps           :", info["fps"])
assert pathlib.Path("meta/modality.json").exists(), "modality.json 없음!"
EOF
```

`total_episodes == len(eps) == npq`, `splits == {"train": "0:<n>"}`, `fps == 25` 여야 한다.

> **📌 옛 문서의 경고는 이제 해당 없다.** [server_finetune_runbook.md](server_finetune_runbook.md)
> Phase 0-3 에는 "`process_dataset.py` 가 `total_videos`/`splits` 를 안 고친다"고 적혀 있는데,
> **현재 코드는 고친다** ([process_dataset.py:407-415](../gear_sonic/scripts/process_dataset.py#L407)).
> 과거에 `splits` 가 `"0:5"` 로 남아 **55개를 병합했는데 5개만 학습되던** 버그가 있었고
> 그때 추가된 수정이다. 그래도 학습 전에 위 점검은 한 번 돌려볼 것 — 틀리면 조용히 데이터가 사라진다.

---

## STEP 5 — train / val 분할

**v2 를 만든 실제 명령** (val 14개는 양성·음성이 고르게 섞이도록 골랐다):

```bash
python gear_sonic/scripts/split_dataset.py \
    --source     outputs/raise_arm_banana_merged_v2 \
    --out-train  outputs/raise_arm_banana_v2_train \
    --out-val    outputs/raise_arm_banana_v2_val \
    --val-episodes 12 24 36 47 49 50 51 52 53 57 61 66 70 75
```

| 산출물 | ep | frame | 원본 `episode_index` |
|---|---|---|---|
| `raise_arm_banana_v2_train` | 65 | 12,383 | 나머지 전부 |
| `raise_arm_banana_v2_val` | 14 | 2,518 | 12, 24, 36, 47, 49, 50, 51, 52, 53, 57, 61, 66, 70, 75 |

<details>
<summary>v1 분할 (참고)</summary>

```bash
python gear_sonic/scripts/split_dataset.py \
    --source     outputs/raise_arm_banana_merged \
    --out-train  outputs/raise_arm_banana_train50 \
    --out-val    outputs/raise_arm_banana_val5 \
    --val-episodes 49 50 51 52 53
```

train50 = ep 0~48 + **54** (50 ep / 10,330 frame), val5 = ep 49~53 (5 ep / 878 frame).
**마지막 5개가 아니라 49~53 이고 54 는 train 에 남는다.**
</details>

| 옵션 | 설명 |
|---|---|
| `--source` | 병합된 원본 |
| `--out-train` / `--out-val` | 출력 경로 |
| `--val-episodes` | val 로 뺄 **원본 `episode_index` 목록** |
| `--overwrite` | 기존 출력 덮어쓰기 |

**원본 `episode_index` 와 파일명을 그대로 보존한다.** 분할 결과가 어느 원본 에피소드인지
항상 추적 가능하다 — 평가([4번 문서](4_evaluation.md))에서 에피소드 번호로 양성/음성을
지정하기 때문에 이 성질이 중요하다.

### ★ `meta/stats.json` — 언제 필요한가

`split_dataset.py` 는 `stats.json` / `relative_stats.json` 을 **만들지 않는다.**

| 용도 | 필요 여부 |
|---|---|
| **학습**(train 셋) | 있으면 그대로 쓴다 |
| **평가**(val 셋, 예측 토큰 덤프) | ⚠️ **미리 만들어야 한다.** 없으면 로더가 죽는다 |

**서버로 전송한 뒤 서버에서 만든다** — `gr00t/data/stats.py` 는 `tyro` CLI 다:

```bash
ssh kist-5090
source ~/groot_env.sh
cd ~/Isaac-GR00T
for D in raise_arm_banana_v2_train raise_arm_banana_v2_val; do
  uv run python gr00t/data/stats.py \
      --dataset-path ~/dataset/$D \
      --embodiment-tag UNITREE_G1_SONIC \
      --modality-config-path gr00t/configs/data/embodiment_configs.py
done
```

`stats.json` 과 `relative_stats.json` 두 개가 생긴다.
`--modality-config-path` 는 **필수다** — `UNITREE_G1_SONIC` 은 내장 태그가 아니라
빼면 `No built-in modality config for embodiment tag ...` 로 죽는다.

v2 에서는 학습 시작(22:39) 전인 **21:21~21:22 에 train·val 둘 다** 만들어 두었다.
새 데이터셋으로 평가를 돌릴 때 이 단계를 빠뜨리면 [4번 문서](4_evaluation.md) STEP 1 에서 막힌다.

### 양성/음성 에피소드 목록 관리

태스크가 조건부("바나나를 **보면** 팔을 올린다")라 **팔을 올리지 않는 음성 에피소드**가 섞여 있다.
이 목록은 알고리즘 자동판정이 아니라 **명시 상수**로 관리한다:

[gear_sonic/scripts/wbc_decoder.py](../gear_sonic/scripts/wbc_decoder.py) 의 `NEGATIVE_EPISODES`
— 현재 `{4, 9, 49} ∪ {55..78}`

**데이터를 새로 수집·병합하면 이 상수를 갱신해야 한다.** 안 하면 평가 지표에서 양성/음성이
섞여 착시가 생긴다(음성은 안 움직이는 게 정답이라 RMSE 가 낮게 나온다).

---

## STEP 6 — GPU 서버로 전송

**train 과 val 을 둘 다 올린다.** 서버 작업 루트는 `/data/data2` 가 아니라 **홈**이다
([3번 문서 §0-2](3_finetune_groot_n17.md#0-2-작업-경로--v1-계획과-v2-실제가-다르다)):

```bash
rsync -avhP --stats \
  ~/GR00T-WholeBodyControl/outputs/raise_arm_banana_v2_train \
  ~/GR00T-WholeBodyControl/outputs/raise_arm_banana_v2_val \
  kist-5090:~/dataset/
```

전송이 끝나면 **서버에서 `stats.json` 을 만든다** ([STEP 5](#-metastatsjson--언제-필요한가)).

이후는 [3. GR00T N1.7 파인튜닝](3_finetune_groot_n17.md) 으로.

---

## `process_dataset.py` 옵션

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--dataset-path` | — | 처리할 데이터셋 1개 이상 (여러 개 = merge) |
| `--dataset-list` | — | 경로 목록 텍스트 파일 (한 줄에 하나, `#` 주석 가능) |
| `--output-path` | — | 출력 경로. **생략 시 제자리 덮어쓰기** |
| `--remove-stale-smpl` / `--no-remove-stale-smpl` | 켜짐 | stale SMPL 프레임 제거. **3-point teleop 수집분은 반드시 끌 것** |
| `--remove-discarded` / `--no-remove-discarded` | 켜짐 | `discarded_episode_indices` 에 있는 에피소드 제거 |

**의존성**: `av`, `numpy`, `pandas`, `tyro` — LeRobot v2.1 on-disk 포맷(parquet + mp4)을
직접 다루므로 학습 프레임워크가 필요 없다.

---

## 함정 정리

| 증상 | 원인 / 조치 |
|---|---|
| **정제 후 프레임이 전부(또는 대량) 사라짐** | 3-point 수집분에 stale SMPL 정제를 적용. `teleop.smpl_pose` 가 항상 all-zero 라 100% 삭제된다 → **`--no-remove-stale-smpl`** |
| 원본이 사라짐 | `--output-path` 생략 → 제자리 덮어쓰기. 항상 지정할 것 |
| `script_config mismatch` | 세션 간 로봇 설정이 다르다. 병합 강행 금지 |
| 정제본 [6] 이 ⚠️ 로 나옴 | **정상**. `episodes_stats.jsonl` 이 없는 정제본의 정해진 동작 (위 갈래 A) |
| 정제본이 404 로 FAIL | 구버전 `verify_dataset.py` 를 쓰고 있다. 이 브랜치 것으로 업데이트 |
| 55개 병합했는데 학습에 5개만 쓰임 | `info.json` 의 `splits` 가 첫 소스 값으로 남은 옛 버그. 현재 코드는 수정됨 — STEP 4 점검으로 확인 |
| 평가에서 val 로더가 죽음 | val 셋에 `meta/stats.json` 없음 → 서버에서 `gr00t/data/stats.py` 로 생성 |
| 평가 지표가 이상하게 좋음 | `NEGATIVE_EPISODES` 미갱신으로 음성이 양성에 섞임 |

---

## 참고 — v2 데이터 구성 실적

| | v1 (`raise_arm_banana_merged`) | **v2** |
|---|---|---|
| train | 50 ep (음성 2, **4%**) | 65 ep (음성 21, **32.3%**) |
| val | 5 ep (음성 1) | 14 ep (음성 6, 42.9%) |
| val `episode_index` | 49~53 | 12, 24, 36, 47, 49, 50, 51, 52, 53, 57, 61, 66, 70, 75 |

**v3 를 준비한다면 — step 이 아니라 양성 데이터를 늘려야 한다.**

| | v1 train | v2 train |
|---|---|---|
| 양성 | 48 ep / 9,981 frame | **44 ep / 9,100 frame** ↓ |
| 음성 | 2 ep / 349 frame (3.4%) | 21 ep / 3,283 frame (26.5%) |

v2 에 **새로 추가된 양성은 0개**다. ep12·24·36·47 이 val 로 빠지면서 양성 학습 데이터가
오히려 881 프레임 줄었다. 음성 비중 26.5% 는 이미 충분하다.
근거는 [eval_results/decoded_openloop_v2_h10/README.md](../eval_results/decoded_openloop_v2_h10/README.md).
