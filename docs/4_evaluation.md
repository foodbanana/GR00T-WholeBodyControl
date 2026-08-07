# 4. 평가 — 관절공간 open-loop & 그래프

학습된 체크포인트가 내놓는 `motion_token`(64차원 latent)을 **WBC decoder(ONNX)로 디코드해
29-DoF 관절 목표(도)** 로 바꾼 뒤, 데이터셋의 **실제 관절값 / 명령값**과 같은 축에 겹쳐 그린다.
**어떤 체크포인트를 실기에 쓸지 고르는 것**이 이 단계의 목적이다.

**이전** ← [3. 파인튜닝](3_finetune_groot_n17.md) · **다음** → [5. 실기 배포](5_deploy.md)

> 방법론·규격의 원본은 [open_loop_eval_decoded_joint.md](open_loop_eval_decoded_joint.md) (259줄),
> v2 실행 순서는 [v2_validation_and_deploy_runbook.md](v2_validation_and_deploy_runbook.md) STEP 1~7.

---

## 0. 왜 관절공간인가

기존 eval 은 `motion_token` 의 **latent MSE** 를 봤다. 스케일 기준이 없어 "MSE 0.006 이
좋은 건지 나쁜 건지" 판단이 불가능했고, 실제로 **과적합을 전혀 잡아내지 못했다**
(v1: ck2000 0.00652 → ck20000 0.00621, 사실상 평평).

관절공간으로 바꾸면 **도(°) 단위**로 나오고, 사람이 그래프를 보고 "이 체크포인트는 팔을
안 든다"를 눈으로 확인할 수 있다.

```
        [GPU 서버]                          [로컬 CPU — GPU 불필요]

  체크포인트 × val 에피소드
         │
         │ ① dump_open_loop_predictions.py   (--execution-horizon 10)
         ▼
   preds/ck<N>/traj_<K>.npz  ──② rsync──▶  ③ eval_decoded_openloop.py
   (gt/pred motion_token)                        │  WBC decoder ONNX
                                                 ▼
                                          decoded.npz + metrics.csv
                                                 │
                                    ┌────────────┴────────────┐
                                    ▼                         ▼
                        ④ plot_decoded_openloop.py   ⑤ plot_checkpoint_sweep.py
                           (에피소드별 3곡선)            (체크포인트 추이 + val/loss)
```

**GPU 는 ① 에서만 쓴다.** 예측 토큰 npz(개당 ~30KB)만 받아오면 나머지는 로컬 CPU 에서 끝난다.

---

## 1. 준비

### 1-1. 로컬 venv (`onnxenv`)

디코드·플롯은 `onnxruntime` / `numpy` / `pyarrow` / `matplotlib` 만 있으면 된다.
학습 프레임워크가 필요 없어 가벼운 venv 를 따로 둔다.

```bash
cd ~/GR00T-WholeBodyControl
python3 -m venv scratchpad/onnxenv
scratchpad/onnxenv/bin/pip install onnxruntime numpy pyarrow matplotlib
```

이하 명령에서 `$SC` = 이 venv 를 둔 디렉터리로 읽는다. 한글 라벨이 네모로 깨지면
`Noto Sans CJK KR` 폰트를 설치한다 (`plot_checkpoint_sweep.py` 가 이 폰트를 지정한다).

### 1-2. WBC decoder ONNX

```
gear_sonic_deploy/policy/release/model_decoder.onnx
IN   obs_dict [1, 994] float32
OUT  action   [1, 29]  float32
```

> **실기에서 도는 것과 같은 디코더를 쓴다.** SONIC 컨트롤러 체크포인트를 바꾸면 같은
> 토큰이 다른 자세로 디코드되므로, 평가와 실기가 같은 `policy/release/` 를 봐야 한다.

### 1-3. val 셋 `meta/stats.json` — 없으면 로더가 죽는다

예측 토큰을 덤프할 때 서버가 val 데이터셋을 로드한다. `split_dataset.py` 는 이 파일을
만들지 않으므로 **미리 생성해야 한다** (서버에서 `gr00t/data/stats.py`).
v2 에서는 학습 전에 만들어 두었다. 자세한 것은 [2번 문서 STEP 5](2_dataset_preprocess_merge.md#-metastatsjson--언제-필요한가).

### 1-4. ★ 음성 에피소드 목록

[gear_sonic/scripts/wbc_decoder.py](../gear_sonic/scripts/wbc_decoder.py) 의 `NEGATIVE_EPISODES`
상수 — 현재 `{4, 9, 49} ∪ {55..78}`.

**데이터셋이 바뀌면 반드시 갱신한다.** 스크립트는 에피소드·체크포인트 목록을 데이터셋과
`--preds-dir` 에서 자동으로 읽지만, **양성/음성 구분만은 이 상수로 명시 지정**한다.

---

## 2. Phase 0 — 검증 게이트 (선행 필수)

> **GT token + GT history 를 decoder 에 넣은 출력이 기록된 `action.wbc` 를 재현하는가?**

재현되면 오프라인 harness 가 정확하다는 뜻이고, 이후 token 만 예측값으로 갈아끼우면 된다.
안 되면 어느 근사가 깨뜨렸는지 여기서 드러난다. **비용은 CPU 몇 초.**

```bash
$SC/onnxenv/bin/python gear_sonic/scripts/eval_decoded_gate.py \
    --dataset outputs/raise_arm_banana_val5
```

두 변형을 비교한다:

- **(a) 25Hz** — 데이터셋 프레임을 그대로. history 10프레임이 200ms 가 아니라 400ms 를 덮는다
- **(b) 50Hz 보간** — 관절/쿼터니언을 50Hz 로 보간해 0.02s 간격 history 를 만든다(decoder 원래 분포).
  실제 시스템도 VLA 가 느리게 토큰을 내고 50Hz decoder 가 반복 소비하므로 실제에 가깝다

### 실측 결과 (2026-08-06, val5 5개 에피소드)

| 변형 | 전체 RMSE | 오른팔 RMSE | MSE |
|---|---|---|---|
| (a) 25Hz | 1.219° | 1.970° | 0.000453 rad² |
| **(b) 50Hz 보간** ✅ | **0.910°** | **1.529°** | **0.000252 rad²** |

**→ (b) 채택.** 이후 모든 평가가 50Hz 보간 + teacher forcing 방식이다.

> ### ★ 측정 하한 = 오른팔 **1.53°**
> 이보다 낮은 오차는 모델 성능이 아니라 **측정 한계**다. 오른팔이 실제로 110~167° 움직이는
> 신호이므로 신호 대비 약 1% — 모델 오차를 재기에 충분한 해상도다.
> 잔차의 원인은 joint velocity·base angular velocity 를 실측이 아니라 차분으로 유도한 것,
> 그리고 25→50Hz 보간이다.

---

## 3. STEP 1 — 예측 토큰 덤프 (서버 GPU)

**`--execution-horizon 10` 을 쓴다.** 실기 재추론 주기와 같아야 하기 때문이다:

```
--rate 2.5               → 0.4초마다 추론
--action-publish-rate 25 → 초당 25개 소비
0.4초 × 25Hz = 10개      → execution_horizon = 10
```

> 이전에 h16(0.64초)으로 뽑은 결과가 [eval_results/decoded_openloop_20260806_h16/](../eval_results/decoded_openloop_20260806_h16/)
> 에 남아 있는데, 실기보다 재추론이 뜸해 **비관적으로 나온다**(평균 23.54° vs h10 20.29°).
> 참고용으로만 본다.

```bash
ssh kist-5090 'bash -lc "
source ~/groot_env.sh; cd ~/Isaac-GR00T
nohup bash -c '\''
OUT=/home/ltw1203/groot_output/rab-v2b-20260806
VAL=/home/ltw1203/dataset/raise_arm_banana_v2_val
for CK in \$(seq 500 500 24000); do
  echo \"=== ck\$CK ===\"
  CUDA_VISIBLE_DEVICES=0 uv run --no-sync python scripts/eval/dump_open_loop_predictions.py dump \
    --model-path \$OUT/checkpoint-\$CK --dataset-path \$VAL \
    --embodiment-tag UNITREE_G1_SONIC --execution-horizon 10 \
    --out-dir /home/ltw1203/eval_v2/preds_h10/ck\$CK 2>&1 | tail -2
done
echo DONE
'\'' > /home/ltw1203/dump_v2.log 2>&1 &
echo launched
"'
```

진행 확인: `ssh kist-5090 'tail -5 ~/dump_v2.log'`

> **48개를 전부 덤프하면 약 2시간**이다. **val/loss 최저 지점 ±2~3개만** 고르면 10분 안에 끝난다.
> 곡선 전체는 로그에 남아 있으므로 나중에 다른 지점을 추가로 덤프해도 된다.
>
> ```bash
> for CK in 3000 3500 4000; do   # 예: 최저가 3500 부근일 때
> ```
>
> 다만 v2 에서 확인된 대로 **val/loss 최저가 관절공간 최적이 아니다**([§8](#8-v2-결과--읽고-넘어가야-할-세-가지)).
> 시간이 되면 넓게 뜨는 편이 낫다.

산출물: `preds_h10/ck<N>/traj_<K>.npz` — 각 npz 에 `gt (T,64)`, `pred (T,64)`.

---

## 4. STEP 2~3 — 가져와서 디코드 (로컬 CPU)

```bash
cd ~/GR00T-WholeBodyControl
SC=scratchpad
rsync -az kist-5090:'/home/ltw1203/eval_v2/preds_h10/' $SC/preds_v2_h10/

$SC/onnxenv/bin/python gear_sonic/scripts/eval_decoded_openloop.py \
    --dataset outputs/raise_arm_banana_v2_val \
    --preds-dir $SC/preds_v2_h10 \
    --out eval_results/decoded_openloop_v2_h10
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--dataset` | 필수 | val 데이터셋 (GT `observation.state` / `action.wbc` 출처) |
| `--preds-dir` | 필수 | `ck*` 하위 폴더에서 **체크포인트 목록을 자동 탐지** |
| `--out` | 필수 | 출력 디렉터리 |
| `--decoder` | `gear_sonic_deploy/policy/release/model_decoder.onnx` | |
| `--checkpoints` | 전체 | 일부만 처리할 때 |
| `--warmup` | `10` | history 미충전 구간(앞 10프레임) 제외 |

**[확인]** 출력 첫 줄의 에피소드 수와 음성 개수가 맞는지 — v2 는 14개 중 음성 6개
(`49, 57, 61, 66, 70, 75`).

산출물: `decoded.npz`(관절 시계열 원본) + `metrics.csv`(ckpt × episode × 관절별 RMSE, `kind` 컬럼 포함).

---

## 5. STEP 4 — 3곡선 플롯 ★

```bash
$SC/onnxenv/bin/python gear_sonic/scripts/plot_decoded_openloop.py \
    --decoded eval_results/decoded_openloop_v2_h10/decoded.npz \
    --out eval_results/decoded_openloop_v2_h10 \
    --all29 --checkpoints 2000 18000 24000
```

> ⚠️ **`--checkpoints` 없이 돌리지 말 것.** `decoded.npz` 에 담긴 체크포인트를 전부 그린다 —
> v2 기준 48 ckpt × 14 ep × 2 = **1,344장**이 된다. `decoded.npz` 는 이미 디코드가 끝난
> 파일이라 플롯은 몇 번이든 다시 뽑을 수 있으니, 먼저 좁게 뽑고 필요할 때 추가한다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--decoded` | — | STEP 3 의 `decoded.npz` (decoder 재실행 없음) |
| `--out` | — | 출력 디렉터리 |
| `--checkpoints` | 전체 | **반드시 지정할 것** |
| `--all29` | 꺼짐 | 29관절 전체 그림도 생성 (기본은 오른팔 7개만) |
| `--fps` | `25` | x축 시간 환산 |
| `--curves` | `all` | `target`(파랑만) / `state`(회색만) 로 줄일 수 있다 |
| `--ylim` | `limit` | y축을 관절 **물리적 한계**(`g1_29dof.xml`)로 고정. `auto` 는 자동 |

### 그래프 읽는 법 — 세 곡선의 의미가 다르다

| 선 | 컬럼 | 의미 |
|---|---|---|
| 회색 `actual joint` | `observation.state` | 로봇이 **실제로 움직인 각도** |
| 파랑 `GT target` | `action.wbc` | 사람이 시연할 때 나간 **명령** — **모델이 맞춰야 할 정답** |
| 빨강 `model` | `decoder(pred_token)` | 모델 예측 토큰을 디코드한 **명령** |

> ### ★ **빨강이 추종해야 하는 것은 파랑이다** (둘 다 명령)
> 회색은 참고선이다. 각 서브플롯 제목의 RMSE 도 **빨강 vs 파랑**이다.
> 회색을 기준으로 읽으면 모델이 실제보다 나쁘게 보인다.

파일명에 `POS`/`NEG` 가 박히고 그림 맨 위에 배너로도 표시된다(음성 주황, 양성 초록).

**RMSE 는 가동범위와 같이 볼 것** — `R_wrist_pitch` 는 가동범위가 12~19° 뿐이라 RMSE 0.5° 가
큰 값일 수 있다. y축이 물리적 한계로 고정돼 있어 관절 간 비교가 가능하다.

### 회색이 파랑을 못 따라가는 이유 — PD 정상상태 오차

데이터셋 **원본**에서 이미 `action.wbc`(명령)와 `observation.state`(실제)가 벌어져 있다:

| 관절 | RMSE | 평균 편차 | 가동폭 대비 |
|---|---|---|---|
| R_shoulder_pitch | 18.6° | **−14.9°** | 12.8% |
| R_shoulder_roll | 12.0° | −9.9° | 29.8% |
| R_elbow | 10.0° | −7.4° | 10.1% |
| 손목 3개 | 1.8~2.8° | ~0 | 5~22% |

**실제 ≈ 명령 × 0.82.** 데이터 수집 결함이 아니라 **PD 정상상태 오차**다:

- 팔이 **멈춰 있는 구간**(속도 5°/s 미만)만 골라도 편차가 그대로 남는다(17.5°) — 시간 지연이면 0 이어야 한다
- 명령을 ±40프레임 밀어 최적 정렬해도 개선이 1~12% 뿐
- 편차가 **중력 부하 순서대로** 정렬된다 (어깨 > 팔꿈치 > 손목)
- `KP = 14.3 N·m/rad` 로 역산한 토크가 평균 3.3 N·m, 최대 10.2 N·m — G1 팔의 중력 토크로 타당

KP 를 올리거나 중력 보상을 넣으면 줄지만 WBC 정책과 안전 설정을 건드리는 일이다.
**조작자가 이 감쇠를 보면서 수집했고 모델이 그 명령을 학습했으므로 실기에서 재현된다.**

---

## 6. STEP 5 — 체크포인트 스윕 (선택의 근거)

```bash
$SC/onnxenv/bin/python gear_sonic/scripts/plot_checkpoint_sweep.py \
    --metrics eval_results/decoded_openloop_v2_h10/metrics.csv \
    --valloss eval_results/decoded_openloop_v2_h10/valloss.tsv \
    --out eval_results/decoded_openloop_v2_h10
```

`--valloss` 는 학습 로그에서 뽑은 `step<TAB>value` 형식 파일이다(선택):

```bash
ssh kist-5090 'grep -oE "step [0-9]+  val/loss = [0-9.]+" ~/train_rab_v2b.log' \
  | sed -E 's/step ([0-9]+)  val\/loss = (.*)/\1\t\2/' \
  > eval_results/decoded_openloop_v2_h10/valloss.tsv
```

`checkpoint_sweep.png` 는 3단(요약+val/loss / 양성별 / 음성별)으로 나오고,
측정 하한 **1.53°** 선이 같이 그려진다.

---

## 7. STEP 6 — 교차 조건 테스트 (판별 능력)

**상태는 고정하고 이미지만 바꿔** 모델이 정말 시각으로 판별하는지 본다.
`--dataset-path` 의 에피소드끼리 이미지를 교차시킨다.

```bash
ssh kist-5090 'bash -lc "
source ~/groot_env.sh; cd ~/Isaac-GR00T
for CK in 18000 24000; do
  CUDA_VISIBLE_DEVICES=0 uv run --no-sync python ~/cross_cond/cross_condition_test.py \
    --model-path ~/groot_output/rab-v2b-20260806/checkpoint-\$CK \
    --dataset-path ~/dataset/raise_arm_banana_v2_val \
    --out-dir ~/cross_cond/v2_ck\$CK
done
"'
```

(리포 사본: [gear_sonic/scripts/cross_condition_test.py](../gear_sonic/scripts/cross_condition_test.py))

> **이 스크립트는 로컬에서 못 돌린다.** `Gr00tPolicy` 로 실제 추론을 하므로 `gr00t` 패키지와
> GPU 가 필요하다 — 서버의 `~/Isaac-GR00T` 환경 안에서 실행한다. CLI 는 `tyro` 라
> 인자 이름은 함수 시그니처를 그대로 따른다(`--model-path`, `--dataset-path`, `--out-dir`,
> `--max-bases 4`, `--max-sources 6`, `--denoising-steps`).
>
> `max_bases`/`max_sources` 로 조합 수를 제한한다 — val 이 14개면 14×14×7 = **1,372 조합**이라
> 그대로 돌리면 과하다. 양성·음성에서 고르게 앞쪽 몇 개만 고른다.
>
> 질의 프레임은 **팔이 아직 올라가기 전(초반)** 으로 잡는다. 팔이 이미 올라간 뒤의 이미지에는
> 팔 자체가 찍혀 있어 "바나나를 봤는가"와 교란된다.
>
> ⚠️ **v2 교차조건 결과는 리포에 기록이 없다.** `eval_results/decoded_openloop_v2_h10/README.md`
> 에도 언급이 없어, STEP 6 이 실제로 돌았는지 불명이다. 서버 `~/cross_cond/` 를 확인할 것.

`NEGATIVE_EPISODES` 를 이 파일도 **자체 복사본으로 들고 있다** — `wbc_decoder.py` 와
**같은 값을 유지해야 한다**(파일 상단 주석에 명시돼 있다). 데이터셋이 바뀌면 두 곳을 다 고친다.

| 결과 | 해석 |
|---|---|
| cross ≫ within **이면서** 바나나 이미지 → 팔 올림 명령 | **판별 학습됨** ✅ |
| cross ≫ within 인데 방향이 무관 | OOD 혼란 (v1 이 이랬다) |
| cross ≈ within | 이미지를 무시하고 있음 |

> v1 결과: cross 가 within 의 2.4~2.8배로 갈렸으나 **방향이 반대**였다(바나나를 넣어도 팔을
> 안 들고 오히려 빈 이미지에서 크게 움직임). 음성이 학습의 4% 뿐이라 학습 불가라는 결론이었고,
> v2 는 32.3% 다.

**조건부 판별의 최종 확인은 실기에서만 가능하다.** 시뮬 렌더링은 실사와 시각 격차가 커서
이 질문에는 답이 안 된다 — 그래서 v2 에서는 MuJoCo sim2sim(STEP 7)을 건너뛰고 실기로 갔다.

---

## 8. v2 결과 — 읽고 넘어가야 할 세 가지

전체는 [eval_results/decoded_openloop_v2_h10/README.md](../eval_results/decoded_openloop_v2_h10/README.md).

### ① ck2000 이후 관절공간 성능이 나아지지 않았다

| ckpt | 양성 8ep | 음성 6ep | val/loss |
|---|---|---|---|
| 1000 | 46.83° | 16.73° | 0.6198 |
| 2000 | 23.18° | 3.20° | 0.2608 |
| 4000 | 29.93° | 2.82° | **0.1044** ← val/loss 최저 |
| 8000 | 24.86° | 1.40° | 0.1269 |
| **18000** | **23.02°** | 1.44° | 0.1880 |
| 24000 | 25.23° | 1.57° | 0.2221 |

ck2000 과 ck18000 의 차이 0.16° 는 **측정 하한 1.53° 보다도 작다.**
22,000 step 을 더 돌린 값어치가 관절공간에서는 나오지 않았다.

### ② ★ val/loss 로 체크포인트를 고르면 안 된다

ck1000(양쪽 다 극단값)을 빼면 두 지표의 상관이 **−0.605 로 방향이 반대**다.
val/loss 최저인 ck4000 이 관절공간에서는 29.93° 로 거의 최악이다.
**val/loss 도 latent 공간에서 재는 값**이라 관절공간 성능과 다른 것을 잰다.

### ③ 평균 23° 는 "75% 성공 / 25% 실패" 다

| ep | 오른팔 RMSE (ck≥2000 평균) |
|---|---|
| ep53 / ep12 / ep51 | 14.9° / 16.4° / 16.8° |
| ep24 / ep52 / ep50 | 19.4° / 22.0° / 22.3° |
| **ep36** | **37.4°** |
| **ep47** | **39.3°** |

ep36·ep47 은 오차가 아니라 **추종 실패**다. `rightarm_only/ep047_POS_ck18000.png` 를 보면
GT 는 t=1~4초에 어깨 pitch 를 −140° 까지 내리는데 모델은 3.5초까지 0° 부근에 머물다
+80° 로 **반대 방향으로 튄다.** 길이·진폭·동작 시점 같은 GT 특성으로는 두 에피소드가
나머지와 구별되지 않는다 — **원인 미상.**

### 결론: **ck18000** 채택

양성 8개 최저(23.02°), 음성 1.44° 로 측정 하한 근처. ck16000 도 사실상 동등하다.
다만 **선택의 실익이 작다** — ck2000 이든 ck18000 이든 23° 다.
실기 결과도 세 체크포인트 모두 과제를 수행해 우열이 없었고, **사전 지표 최저**라는 이유로 정했다
([5번 문서](5_deploy.md#2026-08-07-실기-결과)).

---

## 9. 산출물 구조

```
eval_results/decoded_openloop_v2_h10/
├── README.md                결과 해석 (결론 3가지)
├── checkpoint_sweep.png     체크포인트 추이 (3단: 요약+val/loss / 양성별 / 음성별)
├── metrics.csv              ckpt × episode × 관절별 RMSE (kind 컬럼)
├── decoded.npz              디코드된 관절 시계열 원본 ← 플롯 재생성의 입력
├── valloss.tsv              학습 로그에서 뽑은 val/loss
├── rightarm_only/           에피소드별 오른팔 7관절
└── all29/                   에피소드별 29관절 전체
```

`eval_results/` 아래 다른 폴더:
[decoded_openloop_20260806_h10](../eval_results/decoded_openloop_20260806_h10/) (v1 정식본),
[decoded_openloop_20260806_h16](../eval_results/decoded_openloop_20260806_h16/) (v1 h16, 참고용).

### ★ 그림과 `decoded.npz` 는 git 에 없다 — 클론하면 안 보인다

[.gitignore:223](../.gitignore#L223) 에서 의도적으로 제외한다. 추적하는 것은
**분석 문서(`.md`)와 지표(`.csv`/`.tsv`)뿐**이다.

```
eval_results/**/*.png    ← checkpoint_sweep.png, rightarm_only/, all29/ 전부 제외
eval_results/**/*.npz    ← decoded.npz 도 제외
```

그래서 **README 가 설명하는 그림을 인계받은 사람은 볼 수 없다.** 다시 만들려면 아래
사슬 중 어디까지 남아 있는지에 따라 비용이 달라진다:

| 남아 있는 것 | 필요한 작업 | 비용 |
|---|---|---|
| 로컬에 `decoded.npz` | STEP 4 플롯만 | **수 초** |
| 서버에 `~/eval_v2/preds_h10/` | STEP 2 rsync → STEP 3 디코드 → STEP 4 | 수 분 (CPU) |
| 체크포인트만 | STEP 1 덤프부터 전부 | **~2시간 (GPU)** |

> **먼저 확인할 것**: `ssh kist-5090 'du -sh ~/eval_v2/preds_h10 2>/dev/null; ls ~/eval_v2/preds_h10 | head'`
> 예측 토큰 npz 는 개당 ~30KB 라 전부 합쳐도 작다. **남아 있으면 지우지 말 것.**

---

## 10. 기술 규격 (요약)

전체는 [open_loop_eval_decoded_joint.md](open_loop_eval_decoded_joint.md) §2. 구현은
[gear_sonic/scripts/wbc_decoder.py](../gear_sonic/scripts/wbc_decoder.py).

### obs 994 레이아웃

`observation_config.yaml` 의 `observations:` 나열 순서대로 이어붙인다.
(같은 파일 상단 주석의 `436` 은 **stale** — 4frame 시절 값이다.)

| offset | 블록 | dim | 데이터셋 소스 |
|---|---|---|---|
| 0 | `token_state` | 64 | `action.motion_token` (**GT 또는 모델 예측**) |
| 64 | `his_base_angular_velocity` | 30 | `observation.root_orientation` 쿼터니언 차분 |
| 94 | `his_body_joint_positions` | 290 | `observation.state` |
| 384 | `his_body_joint_velocities` | 290 | `observation.state` 차분 |
| 674 | `his_last_actions` | 290 | `action.wbc` |
| 964 | `his_gravity_dir` | 30 | `observation.root_orientation` 에서 계산 |

history 는 **oldest-first**, 블록 내부는 `[frame][joint]` (frame-major).

### 43 → 29 관절

```
left_leg 6 | right_leg 6 | waist 3 | left_arm 7 | left_hand 7 | right_arm 7 | right_hand 7
0        6 12          15        22          29           36            43
```

손 14개를 빼면 **인덱스 `[0:22] + [29:36]`**. 오른팔은 29-vector 에서 `[22:29]`.

### ★ 관절 순서와 단위 — 가장 헷갈리는 부분

C++ **내부** 상태와 **ZMQ 로 내보낸** 값(=데이터셋 기록값)이 다르다.
내부는 **isaaclab 순서 + default_angles 차감**이고, 발행 시 되돌린다.
따라서 **데이터셋에 있는 값은**:

| 컬럼 | 의미 |
|---|---|
| `observation.state`[body29] | **절대 관절각 (rad), mujoco 순서** |
| `action.wbc`[body29] | **관절목표 (rad), mujoco 순서** |

`action.wbc` 는 raw 네트워크 출력이 아니라 **이미 스케일·오프셋이 적용된 관절목표**다.
덕분에 `observation.state` 와 같은 단위·같은 순서라 **바로 겹쳐 그릴 수 있다.**

(`g1_deploy_onnx_ref.cpp` 의 `// URDF order` 주석은 오해를 부른다 — isaaclab 순서다.)

---

## 11. 함정 정리

| 증상 | 원인 / 조치 |
|---|---|
| 덤프 중 val 로더가 죽음 | val 셋에 `meta/stats.json` 없음 → `gr00t/data/stats.py` 로 생성 |
| 401 `Cosmos-Reason2-2B` | `source ~/groot_env.sh` 누락 ([3번 문서 §0-3](3_finetune_groot_n17.md#0-3--groot_envsh--모든-명령-앞에-붙는다)) |
| 플롯이 1,344장 쏟아짐 | `--checkpoints` 미지정 |
| 음성 에피소드 RMSE 가 매우 낮음 | **정상** — 안 움직이는 게 정답이다. 성능 판단은 **양성만**으로 |
| 지표가 이상하게 좋음 | `NEGATIVE_EPISODES` 미갱신으로 음성이 섞임 |
| 빨강이 회색을 못 따라감 | **정상** — 빨강의 기준은 파랑이다. 회색과의 간격은 PD 감쇠(×0.82) |
| RMSE 가 1.5° 이하 | 측정 하한. 모델 성능 차이로 읽지 말 것 |
| h16 결과와 숫자가 다름 | `--execution-horizon` 이 다르다. **실기 기준은 h10** |
| val/loss 최저를 골랐는데 실기가 나쁨 | 알려진 현상. 상관 −0.605 ([§8-②](#8-v2-결과--읽고-넘어가야-할-세-가지)) |
| 그래프 한글이 네모 | `Noto Sans CJK KR` 미설치 |
| 클론했는데 그림/`decoded.npz` 가 없음 | **정상** — gitignore 대상이다. [§9](#-그림과-decodednpz-는-git-에-없다--클론하면-안-보인다) 의 재생성 사슬 |
| `cross_condition_test.py` 가 import 에러 | 로컬에서 돌린 것. `gr00t` 패키지가 있는 **서버**에서 실행 |
| 양성/음성 판정이 두 스크립트에서 다름 | `wbc_decoder.py` 와 `cross_condition_test.py` 의 `NEGATIVE_EPISODES` 불일치 |
