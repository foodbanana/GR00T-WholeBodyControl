# v2 관절공간 open-loop 평가 — `rab-v2b-20260806`

학습된 VLA가 내놓는 `motion_token`(64차원)을 **WBC decoder(ONNX)로 디코드해
29-DoF 관절 목표(도)** 로 바꾼 뒤, 데이터셋의 실제 관절값과 같은 그래프에서 비교한 것.
방법론은 [`../../docs/open_loop_eval_decoded_joint.md`](../../docs/open_loop_eval_decoded_joint.md).

- 모델: `rab-v2b-20260806` (24000 step, GPU 4장, 배치 32, 2026-08-07 완료)
- 검증셋: `raise_arm_banana_v2_val` — 14 에피소드 (**양성 8 + 음성 6**)
- 체크포인트 19개: 1000~14000(1000 간격) + 16000, 18000, 20000, 22000, 24000
- `--execution-horizon 10` — 실기 재추론 주기(0.4초 × 25Hz)와 같다

## 음성(NEGATIVE) 에피소드

바나나가 없으므로 **팔이 가만히 있어야 정답**인 에피소드다. RMSE가 낮은 것은
잘해서가 아니라 원래 안 움직이기 때문이므로, **성능 판단은 양성 8개로 한다.**

| ep | 종류 | 비고 |
|---|---|---|
| 12, 24, 36, 47, 50, 51, 52, 53 | POS (양성) | 성능 판단은 이 8개로 |
| **49** | **NEG** | **흰 줄 없는 대조군** — 아래 참조 |
| 57, 61, 66, 70, 75 | NEG | 2026-08-06 추가 수집분 (바닥에 흰 줄 있음) |

음성 목록은 알고리즘 자동판정이 아니라 `gear_sonic/scripts/wbc_decoder.py` 의
`NEGATIVE_EPISODES` 상수(`{4,9,49} ∪ {55..78}`)로 **명시 지정**한다.

**흰 줄 대조군**: 추가 수집한 음성 24개는 바닥에 흰 줄이 그어진 상태로 찍혔다.
모델이 바나나가 아니라 흰 줄을 보고 판별할 위험이 있어, 흰 줄이 없는 **ep49만
대조군**이 된다. 결과적으로 ep49도 나머지 음성과 같은 수준(1.4~4.3°)이라
**그 우려는 해소됐다.**

## 결론 세 가지

### 1. ck2000 이후 관절공간 성능이 나아지지 않았다

| ckpt | 양성 8ep | 음성 6ep | val/loss |
|---|---|---|---|
| 1000 | 46.83 | 16.73 | 0.6198 |
| 2000 | 23.18 | 3.20 | 0.2608 |
| 4000 | 29.93 | 2.82 | **0.1044** ← val/loss 최저 |
| 8000 | 24.86 | 1.40 | 0.1269 |
| 12000 | 27.21 | 1.65 | 0.1363 |
| **18000** | **23.02** | 1.44 | 0.1880 |
| 24000 | 25.23 | 1.57 | 0.2221 |

ck2000(23.18°)과 ck18000(23.02°)의 차이 0.16°는 **측정 하한 1.53° 보다도 작다.**
22,000 step 을 더 돌린 값어치가 관절공간에서는 나오지 않았다.

### 2. val/loss 로 체크포인트를 고르면 안 된다

ck1000(양쪽 다 극단값)을 빼면 두 지표의 상관이 **−0.605 로 방향이 반대다.**
val/loss 최저인 ck4000 은 관절공간에서 29.93° 로 거의 최악이다.
val/loss 도 `motion_token` 잠재공간에서 재는 값이라 관절공간 성능과 다른 것을 잰다.
v1 에서 latent MSE 가 과적합을 못 잡았던 것과 같은 현상이다.

### 3. 평균 23° 는 "75% 성공 / 25% 실패" 다

| ep | 오른팔 RMSE (ck≥2000 평균) |
|---|---|
| ep53 | 14.9° |
| ep12 | 16.4° |
| ep51 | 16.8° |
| ep24 | 19.4° |
| ep52 | 22.0° |
| ep50 | 22.3° |
| **ep36** | **37.4°** |
| **ep47** | **39.3°** |

ep36·ep47 은 오차가 아니라 **추종 실패**다. `rightarm_only/ep047_POS_ck18000.png`
를 보면 GT 는 t=1~4초에 어깨 pitch 를 −140° 까지 내리는데 모델은 3.5초까지 0° 부근에
머물다 +80° 로 반대 방향으로 튄다. 길이·진폭·동작 시점 같은 GT 특성으로는 두
에피소드가 나머지와 구별되지 않는다.

## 권장 체크포인트: **ck18000**

양성 8개 최저(23.02°), v1 과 겹치는 ep50~53 기준으로도 최저(14.29°, v1 최선 15.95°보다 낫다),
음성 1.44° 로 측정 하한 근처. ck16000(23.47°/1.36°)도 사실상 동등하다.

다만 **선택의 실익이 작다** — ck2000 이든 ck18000 이든 23° 다.

## 명령 vs 실제 관절 (회색선이 파란선을 못 따라가는 이유)

데이터셋 원본에서 `action.wbc`(명령)와 `observation.state`(실제)의 차이:

| 관절 | RMSE | 평균 편차 | 가동폭 대비 |
|---|---|---|---|
| R_shoulder_pitch | 18.6° | **−14.9°** | 12.8% |
| R_shoulder_roll | 12.0° | −9.9° | 29.8% |
| R_elbow | 10.0° | −7.4° | 10.1% |
| 손목 3개 | 1.8~2.8° | ~0 | 5~22% |

**실제 ≈ 명령 × 0.82.** 이것은 데이터 수집 결함이 아니라 **PD 정상상태 오차**다:

- 팔이 **멈춰 있는 구간**(속도 5°/s 미만)만 골라도 편차가 그대로 남는다(17.5°) — 시간 지연이면 0 이어야 한다
- 명령을 ±40프레임 밀어 최적 정렬해도 개선이 1~12% 뿐이다
- 편차가 **중력 부하 순서대로 정렬**된다 (어깨 > 팔꿈치 > 손목)
- `KP = 14.3 N·m/rad` 에서 역산한 토크가 평균 3.3 N·m, 최대 10.2 N·m — G1 팔의 중력 토크로 타당하다

KP 를 올리거나 중력 보상을 넣으면 줄지만, WBC 정책과 안전 설정을 건드리는 일이라
이번 과제에는 불필요하다. 조작자가 이 감쇠를 보면서 수집했고 모델이 그 명령을
학습했으므로 실기에서 재현된다.

## 파일

```
checkpoint_sweep.png    체크포인트 19개 추이 (3단: 요약+val/loss / 양성별 / 음성별)
metrics.csv             ckpt × episode × 관절별 RMSE (kind 컬럼 포함)
decoded.npz             디코드된 관절 시계열 원본
valloss.tsv             학습 로그에서 뽑은 val/loss 120개
rightarm_only/          에피소드별 오른팔 7관절 (ck2000/18000/24000)
all29/                  에피소드별 29관절 전체
```

파일명에 `POS`/`NEG` 가 박혀 있고 그림 맨 위에도 배너로 표시된다(음성 주황, 양성 초록).

## 그래프 읽는 법

| 선 | 의미 |
|---|---|
| 회색 `actual joint (observation.state)` | 로봇이 실제로 움직인 각도 |
| 파랑 `GT target (action.wbc)` | 사람이 시연할 때 나간 **명령** — 모델이 맞춰야 할 정답 |
| 빨강 `model (decoded pred token)` | 모델 예측 토큰을 디코드한 **명령** |

**빨강이 추종해야 하는 것은 파랑이다** (둘 다 명령). 회색은 참고선이며, 파랑과
회색의 간격이 위에서 설명한 PD 감쇠다.

각 서브플롯 제목의 RMSE 는 빨강 vs 파랑이다. y축은 그 관절의 **물리적 한계**
(`g1_29dof.xml`)로 고정돼 있어 관절 간 비교가 가능하다. RMSE 는 가동범위와 같이
볼 것 — `R_wrist_pitch` 는 가동범위가 12~19° 뿐이라 RMSE 0.5° 가 큰 값일 수 있다.

## 재생성

```bash
# 1. 서버에서 예측 토큰 덤프 (GPU)
ssh kist-5090 'bash ~/dump_v2_full.sh'

# 2. 로컬로 가져와 디코드 (CPU)
rsync -az kist-5090:'/home/<USER>/eval_v2/preds_h10/' $SC/preds_v2_h10/
$SC/onnxenv/bin/python gear_sonic/scripts/eval_decoded_openloop.py \
    --dataset outputs/raise_arm_banana_v2_val --preds-dir $SC/preds_v2_h10 \
    --out eval_results/decoded_openloop_v2_h10

# 3. 그림
$SC/onnxenv/bin/python gear_sonic/scripts/plot_decoded_openloop.py \
    --decoded eval_results/decoded_openloop_v2_h10/decoded.npz \
    --out eval_results/decoded_openloop_v2_h10 --all29 \
    --checkpoints 2000 18000 24000
$SC/onnxenv/bin/python gear_sonic/scripts/plot_checkpoint_sweep.py \
    --metrics eval_results/decoded_openloop_v2_h10/metrics.csv \
    --valloss eval_results/decoded_openloop_v2_h10/valloss.tsv \
    --out eval_results/decoded_openloop_v2_h10
```

`$SC/onnxenv` 는 onnxruntime·numpy·pyarrow·matplotlib 만 있는 가벼운 venv 다.

## 다음 학습(v3)에 대한 함의

**step 을 늘리는 것이 아니라 양성 데이터를 늘려야 한다.**

| | v1 train | v2 train |
|---|---|---|
| 양성 | 48 ep / 9,981 frame | **44 ep / 9,100 frame** ↓ |
| 음성 | 2 ep / 349 frame (3.4%) | 21 ep / 3,283 frame (26.5%) |

v2 에 **새로 추가된 양성은 0개**다. ep12·24·36·47 이 val 로 빠지면서 양성 학습
데이터가 오히려 881 프레임 줄었다. 음성 비중 26.5% 는 이미 충분하다.
