# v2 검증 및 실기 적용 RUNBOOK

학습된 체크포인트를 **검증**(STEP 1~7)하고 **실제 로봇에 적용**(STEP 8)하기까지의 실행 순서.

`rab-v2b-20260806` 학습(24000 step, 2026-08-07 09:15 경 완료 예정)이 끝나면
위에서부터 그대로 복붙하면 된다.
평가 방법론은 [`open_loop_eval_decoded_joint.md`](open_loop_eval_decoded_joint.md) 참조.

## 무엇이 v1과 다른가

| | v1 | **v2** |
|---|---|---|
| train | 50 ep (음성 2, **4%**) | 65 ep (음성 21, **32.3%**) |
| val | 5 ep (음성 **1**) | 14 ep (음성 **6**, 42.9%) |
| 체크포인트 | 2000~20000 (6개) | 500~24000 (**48개**) |
| val episode_index | 49~53 | **12, 24, 36, 47, 49, 50, 51, 52, 53, 57, 61, 66, 70, 75** |

**v1에서 결론을 못 낸 두 가지가 v2에서 처음으로 측정된다:**
- 체크포인트 순위 — v1은 양성 4개뿐이라 ep52 하나가 순위를 흔들었다
- **바나나 판별 능력** — v1은 음성이 학습의 4%뿐이라 애초에 학습 불가였다

스크립트는 전부 **데이터셋에서 에피소드·체크포인트를 자동으로 읽는다.** v2 전용
수정은 필요 없다. 음성 목록만 `gear_sonic/scripts/wbc_decoder.py` 의
`NEGATIVE_EPISODES` 상수로 관리한다(현재 `{4,9,49} ∪ {55..78}`, 갱신 완료).

---

## STEP 1 — 학습 완료 확인 (서버)

```bash
ssh kist-5090 'bash -s' <<'EOF'
grep -oE "[0-9]+/24000 \[[0-9:]+<[0-9:]+" ~/train_rab_v2b.log | tail -1
echo "--- val/loss 전체 ---"
grep -oE "step [0-9]+  val/loss = [0-9.]+" ~/train_rab_v2b.log
echo "--- 체크포인트 ---"; ls ~/groot_output/rab-v2b-20260806/ | grep checkpoint | sort -t- -k2 -n | tr '\n' ' '
echo; echo "--- 에러 ---"; grep -cE "Traceback|CUDA out of memory" ~/train_rab_v2b.log
EOF
```

**[확인]** `24000/24000` 도달, 에러 0, 체크포인트 48개.

> 48개를 전부 덤프하면 오래 걸린다. **val/loss 최저 지점 ±2~3개만** 골라서 STEP 2를
> 돌리는 편이 낫다. 곡선 전체는 로그에 남아 있으므로 나중에 다른 지점을 추가로
> 덤프해도 된다.
**val/loss 최저 지점을 기록해 둘 것** — 이후 해석의 기준이 된다.

## STEP 2 — 예측 토큰 덤프 (서버 GPU 0)

실기와 같은 재추론 주기(`--execution-horizon 10`)를 쓴다. 근거는
[`../eval_results/decoded_openloop_20260806_h10/README.md`](../eval_results/decoded_openloop_20260806_h10/README.md).

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

**48개를 전부 덤프하면 약 2시간** 걸린다(v1은 6ckpt×5ep 에 4분). val/loss 최저
지점 ±2~3개만 고르면 10분 안에 끝난다. 위 `seq` 를 원하는 목록으로 바꿔 쓸 것:

```bash
for CK in 3000 3500 4000; do   # 예: 최저가 3500 부근일 때
```
진행 확인: `ssh kist-5090 'tail -5 ~/dump_v2.log'`

> `raise_arm_banana_v2_val` 의 `meta/stats.json` 은 학습 전에 생성해 두었다.
> 새 데이터셋을 쓸 때는 `gr00t/data/stats.py` 로 먼저 만들어야 로더가 죽지 않는다.

## STEP 3 — 로컬로 가져오기

```bash
cd /home/edgexpert00/GR00T-WholeBodyControl
SC=<scratchpad>
rsync -az kist-5090:'/home/ltw1203/eval_v2/preds_h10/' $SC/preds_v2_h10/
```

## STEP 4 — 디코드 (로컬 CPU, GPU 불필요)

```bash
$SC/onnxenv/bin/python gear_sonic/scripts/eval_decoded_openloop.py \
    --dataset outputs/raise_arm_banana_v2_val \
    --preds-dir $SC/preds_v2_h10 \
    --out eval_results/decoded_openloop_v2_h10
```

체크포인트 목록은 `--preds-dir` 의 `ck*` 에서 자동 탐지된다.
에피소드 번호·음성 여부는 데이터셋과 `NEGATIVE_EPISODES` 에서 읽는다.

**[확인]** 출력 첫 줄의 에피소드 14개와 음성 6개(`49, 57, 61, 66, 70, 75`)가 맞는지.

## STEP 5 — 플롯

```bash
$SC/onnxenv/bin/python gear_sonic/scripts/plot_decoded_openloop.py \
    --decoded eval_results/decoded_openloop_v2_h10/decoded.npz \
    --out eval_results/decoded_openloop_v2_h10 --all29
```

48 ckpt × 14 ep × 2 = **1,344장**이 되므로 반드시 추려서 뽑는다:

```bash
    --checkpoints 3000 3500 4000     # 덤프한 것만
```

생략하면 `decoded.npz` 에 담긴 체크포인트 전부를 그린다.

## STEP 6 — ★교차 조건 테스트★ (서버 GPU 0)

**v2에서 가장 중요한 검증.** 상태는 고정하고 이미지만 바꿔 판별 능력을 본다.

```bash
ssh kist-5090 'bash -lc "
source ~/groot_env.sh; cd ~/Isaac-GR00T
for CK in <val/loss 최저 ckpt> 24000; do
  CUDA_VISIBLE_DEVICES=0 uv run --no-sync python ~/cross_cond/cross_condition_test.py \
    --model-path ~/groot_output/rab-v2b-20260806/checkpoint-\$CK \
    --dataset-path ~/dataset/raise_arm_banana_v2_val \
    --out-dir ~/cross_cond/v2_ck\$CK
done
"'
```

**판정 기준**

| 결과 | 해석 |
|---|---|
| cross ≫ within **이면서** 바나나 이미지 → 팔 올림 명령 | **판별 학습됨** ✅ |
| cross ≫ within 인데 방향이 무관 | OOD 혼란 (v1이 이랬다) |
| cross ≈ within | 이미지를 무시하고 있음 |

v1 결과: cross가 within의 2.4~2.8배로 갈렸으나 **방향이 반대**였다(바나나 이미지를
넣어도 팔을 안 들고 오히려 빈 이미지에서 크게 움직임). 음성 4%로는 학습 불가라는
결론이었고, v2는 32%다.

## STEP 7 — MuJoCo 폐루프 안전 확인 (로컬 CPU)

open-loop 지표가 "토큰이 얼마나 정확한가"를 재는 반면, 여기서는 **그 토큰을 실제로
이어서 실행하면 어떻게 움직이는가**를 본다. open-loop 은 매 프레임 이력을 GT로
재고정하므로 발산·낙상을 볼 수 없다.

### 7-1. 대조군 먼저 (필수)

```bash
$SC/onnxenv/bin/python gear_sonic/scripts/closed_loop_mujoco.py \
    --preds-dir  $SC/preds_v2_h10 \                    # 덤프된 토큰 폴더
    --dataset    outputs/raise_arm_banana_v2_val \     # 라벨(ep번호·POS/NEG) 출처
    --checkpoints <best> \                             # val/loss 최저 체크포인트
    --trajs 0 1 \                                      # 순번 — 아래 표 참조
    --source gt                                        # ★정답 토큰 = 대조군
```

**[확인]** GT로 넘어지지 않고, 양성 traj 의 오른팔 가동범위가 데이터셋 실측과
비슷해야 한다(v1: 시뮬 163.1° vs 실측 159.5°). **여기서 어긋나면 모델이 아니라
harness 문제**이므로 다음으로 넘어가지 말 것.

### 7-2. 본 검증

```bash
$SC/onnxenv/bin/python gear_sonic/scripts/closed_loop_mujoco.py \
    --preds-dir  $SC/preds_v2_h10 \
    --dataset    outputs/raise_arm_banana_v2_val \
    --checkpoints <best> 24000 \                       # 최저 지점 + 마지막
    --trajs 0 1 4 8 \                                  # 음성·양성 섞어서
    --source pred \                                    # 모델 예측 토큰
    --chunk-blend-frames 3                             # 실기 블렌딩 반영(선택)
```

**[확인]** 넘어짐 0, 관절한계 위반 0, 토크포화 1% 미만.
오른팔 가동범위를 7-1의 GT와 비교 — v1은 GT 163° 대비 예측 103~133°로 **20~37% 부족**했다.

### 7-3. 눈으로 보기

```bash
# 창을 띄워 실시간 재생 (DISPLAY 필요)
... --viewer

# 영상으로 저장 (헤드리스 가능, --viewer 와 동시 사용 가능)
... --video --video-size 960 720
#   → eval_results/closed_loop_mujoco/video/{source}_ck{N}_traj{K}.mp4
```

`--source gt` 와 `--source pred` 를 각각 돌려 나란히 보면 "팔을 얼마나 덜 드는지"가
수치보다 빠르게 파악된다.

### v2_val 의 traj 순번 ↔ 에피소드

`--trajs` 는 **에피소드 번호가 아니라 데이터셋 안 순번**이다.

| traj | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ep | 12 | 24 | 36 | 47 | **49** | 50 | 51 | 52 | 53 | **57** | **61** | **66** | **70** | **75** |
| 종류 | POS | POS | POS | POS | **NEG** | POS | POS | POS | POS | **NEG** | **NEG** | **NEG** | **NEG** | **NEG** |

`--dataset` 을 주면 터미널에 `ep49 NEG` 형태로 찍히므로 확인 가능하다.

**흰 줄 대조군**: 음성 6개 중 **traj4(ep49)만 바닥에 흰 줄이 없다.** traj4 와
traj9~13 을 비교해 ep49 에서만 성능이 나쁘면 모델이 바나나가 아니라 **흰 줄**을
보고 판별하는 것이다.

---

---

# STEP 8 — 실기 배포

공식 가이드: <https://nvlabs.github.io/GR00T-WholeBodyControl/tutorials/vla_inference.html>
아래는 **우리 환경에 맞춘 차이점 위주**로, 공식 문서 그대로 하면 안 되는 부분을 표시했다.

## 8-0. 사전 조건

| 항목 | 상태 (2026-08-06 확인) | 조치 |
|---|---|---|
| C++ deploy 바이너리 | ✅ **빌드됨** — `gear_sonic_deploy/target/release/g1_deploy_onnx_ref` (2026-07-23) | 없음 |
| **`.venv_inference`** | ❌ **없음** | `bash install_scripts/install_inference.sh` |
| PolicyServer 구동 위치 | 미정 | 8-1 참조 |
| 카메라 서버 | 로봇 전원 필요 | 로봇 온보드에서 docker 실행 |
| 로봇 네트워크 | 미도달(전원 off) | `ping 192.168.123.164` |

`.venv_inference` 설치는 **로봇 없이 미리** 해둘 수 있다. 당일 시간을 아끼려면 먼저 할 것.

## 8-1. PolicyServer 를 어디서 돌릴 것인가 — 결정 필요

| 안 | 장점 | 문제 |
|---|---|---|
| **A. DGX Spark(로컬)** | 네트워크 지연 없음 | **aarch64(ARM64) + GB10** 이라 Isaac-GR00T 의존성 설치 미검증. 로컬 `~/Isaac-GR00T` 는 clone 만 되어 있고 `.venv` 없음 |
| **B. KIST 서버** | 이미 설치·검증됨 | 원격이라 네트워크 지연이 추론 지연에 더해짐. SSH 터널 필요 |

**A 를 먼저 시도**하고 안 되면 B. A 검증:

```bash
cd ~/Isaac-GR00T && uv sync --all-extras
uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

B 로 갈 경우 터널을 켜둔 채로 둔다:

```bash
ssh -N -L 5550:localhost:5550 kist-5090
```

## 8-2. 체크포인트 준비

STEP 1에서 고른 **val/loss 최저 체크포인트**를 쓴다.

```bash
CKPT=/home/ltw1203/groot_output/rab-v2b-20260806/checkpoint-<best>

# A(로컬)로 가져올 때 — 34GB
rsync -avhP kist-5090:$CKPT ~/models/
```

## 8-3. 구조도

터미널 번호는 아래 8-4 의 실행 순서와 같다. 포트는 전부 기본값이다.

```
   [로봇 온보드]                          [DGX Spark]                        [GPU 머신]

┌────────────────────┐
│ ① Camera Server    │
│   docker (Orin)    │
│   192.168.123.164  │
└─────────┬──────────┘
          │ ZMQ  :5555  (ego_view 이미지)
          │
          ├──────────────────────────────────────────┐
          │                                          │
          ▼                                          ▼
┌────────────────────────────┐  ZMQ REQ/REP  ┌──────────────────────┐
│ ⑤ VLA Inference            │  :5550        │ ② PolicyServer       │
│   run_vla_inference.py     │◄─────────────►│   run_gr00t_server   │
│                            │  관측→토큰    │   (파인튜닝 모델)     │
│   --action-publish-rate 25 │               │   --device cuda:0    │
│   --chunk-blend-frames 3   │               └──────────────────────┘
└──┬──────────────────▲──────┘
   │ ZMQ PUB :5556    │ ZMQ SUB :5557
   │ (motion_token)   │ (로봇 상태 g1_debug)
   ▼                  │
┌─────────────────────┴──────┐         ┌──────────────────────┐
│ ③ C++ Deploy               │◄────────┤ ④ Keyboard Publisher │
│   g1_deploy_onnx_ref       │  :5580  │   keyboard_publisher │
│   --input-type zmq_manager │    ▲    │   k / i / p / t      │
│                            │    └────┼──────────────────────┘
│   WBC decoder 50Hz         │         └── ⑤ VLA Inference 도 같은 :5580 구독
│   994-dim obs → 29 action  │
└─────────┬──────────────────┘
          │ DDS (unitree SDK)
          ▼
┌────────────────────┐
│   G1 로봇 (실기)    │
│   29 DoF + Dex3    │
└────────────────────┘

                         ┌──────────────────────────────┐
   ①,③ 에서 ────────────►│ ⑥ Data Exporter (선택)       │
   (이미지 + 상태)        │   run_data_exporter.py       │
                         │   → LeRobot 데이터셋 녹화     │
                         └──────────────────────────────┘
```

### 데이터 흐름 요약

| 구간 | 프로토콜 | 포트 | 내용 |
|---|---|---|---|
| ① → ⑤, ⑥ | ZMQ | 5555 | `ego_view` 카메라 이미지 |
| ⑤ ↔ ② | ZMQ REQ/REP | 5550 | 관측 → **motion_token 40개 chunk** |
| ⑤ → ③ | ZMQ PUB | 5556 | motion_token 1개씩 (**25Hz**) |
| ③ → ⑤, ⑥ | ZMQ SUB | 5557 | 로봇 상태 (`g1_debug`) |
| ④ → ③, ⑤ | ZMQ PUB | 5580 | 키 입력 `k`/`i`/`p`/`t` |
| ③ → 로봇 | DDS | — | 관절 목표 (50Hz) |

### 데이터 수집 때와 무엇이 다른가

```
[데이터 수집]  사람 → PICO VR → pico_manager_thread_server.py ─┐
                                                              ├─► ③ C++ Deploy → 로봇
[VLA 추론]     카메라 → ⑤ VLA Inference ↔ ② PolicyServer ─────┘
```

**⑤ 가 `pico_manager_thread_server.py` 자리를 대체**하고 **② 가 새로 추가**된다.
사람이 조종하던 것을 모델이 대신하는 것이므로 **둘을 동시에 띄우면 안 된다.**

**④ 키보드 publisher 는 양쪽 모두에 필요하다.** tmux 런처는 이걸 pane 안에서
자동으로 띄우지만, 터미널을 직접 여는 경우 **빠뜨리면 `k`/`i`/`p` 가 아무 데도 가지 않는다.**

## 8-4. 실행 — 터미널 6개 (tmux 없이)

기존 데이터 수집 때와 **다른 점은 두 가지**다.
`pico_manager_thread_server.py`(사람이 VR로 조종) 자리에 **`run_vla_inference.py`**(모델이
조종) 가 들어가고, **PolicyServer 터미널이 하나 추가**된다. 둘을 동시에 띄우면 안 된다.

키보드 publisher 는 tmux 런처가 내부에서 띄우던 것이라 **수동 실행 시 직접 띄워야 한다.**

### Terminal 1 — 카메라 서버 (로봇 온보드)

```bash
ping -c 3 192.168.123.164
ssh unitree@192.168.123.164
# 로봇에서 docker/run_ltw_camera_server_ros2foxy_v8.sh 실행
```

### Terminal 2 — PolicyServer (GPU 있는 곳)

```bash
cd ~/Isaac-GR00T
uv run python gr00t/eval/run_gr00t_server.py \
    --model-path <체크포인트 경로> \
    --embodiment-tag UNITREE_G1_SONIC \
    --device cuda:0 --port 5550
```

### Terminal 3 — C++ Deploy (DGX Spark)

데이터 수집 때와 **완전히 동일**하다.

```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
source scripts/setup_env.sh
./deploy.sh --input-type zmq_manager real
```

### Terminal 4 — 키보드 publisher ★수동 실행 시 빠뜨리기 쉬움★

```bash
cd ~/GR00T-WholeBodyControl
source .venv_inference/bin/activate
python gear_sonic/scripts/keyboard_publisher.py
```

C++ deploy 와 `run_vla_inference.py` 가 **둘 다 이 포트(5580)를 구독**한다.
여기에 키를 입력하면 양쪽이 동시에 반응한다.

| 키 | 동작 |
|---|---|
| `k` | 제어루프 시작 / 정지 |
| `i` | 초기 자세로 블렌딩 |
| `p` | 추론 일시정지 / 재개 |
| `[` `]` | 손 토글 |
| `t <문장>` | **프롬프트 교체 (실행 중 가능)** |

### Terminal 5 — VLA Inference ★공식 문서와 다른 인자 있음★

```bash
cd ~/GR00T-WholeBodyControl
source .venv_inference/bin/activate
python gear_sonic/scripts/run_vla_inference.py \
    --host <PolicyServer IP> --port 5550 \
    --embodiment-tag unitree_g1_sonic \
    --prompt "raise your right arm if you see a banana" \
    --camera-host 192.168.123.164 --camera-port 5555 \
    --action-publish-rate 25 \
    --chunk-blend-frames 3
```

| 인자 | 공식 기본값 | **우리 값** | 이유 |
|---|---|---|---|
| `--action-publish-rate` | 50 | **25** | 모델이 **25fps** 데이터로 학습됐다. `delta_indices=range(40)` 이 데이터셋 프레임 40개 연속이라 리샘플링이 없어, 50이면 **동작이 2배 속도로 재생**된다 |
| `--chunk-blend-frames` | (없음) | **3** | 우리가 추가한 옵션. chunk 경계에서 관절목표가 평균 20°(최대 204°) 튀는 것을 완화. 이상하면 `0` 으로 즉시 원복 |
| `--prompt` | `demo` | 학습과 **동일 문장** | 다르면 모델이 이미지 대신 프롬프트로 판별할 여지가 생긴다 |

### Terminal 6 — Data Exporter (선택, 권장)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_data_exporter.py \
    --task-prompt "raise your right arm if you see a banana" \
    --camera-host 192.168.123.164 --camera-port 5555 \
    --use-nvenc --camera-triggered --camera-decode-reduce 1 --dataset-fps 25 \
    2>&1 | tee vla_run_$(date +%Y%m%d_%H%M%S).log
```

실기 실행을 녹화해두면 나중에 open-loop 으로 재분석할 수 있다.
녹화는 키보드 publisher 의 `c` 로 시작/정지한다.

## 8-5. 조작 순서

1. Terminal 1~6 을 위 순서대로 띄운다 (카메라 → PolicyServer → deploy → 키보드 → VLA → 녹화)
2. Terminal 3 (C++ Deploy) 에서 Enter 로 배포 확인
3. **Terminal 4 (키보드 publisher)** 에 타이핑한다
4. **`k`** — C++ 제어루프 시작 (PLANNER 모드)
5. **`i`** — 초기 자세로 블렌딩 (POSE 모드, 1초에 걸쳐 이동)
6. **`p`** — 추론 루프 시작 → **로봇이 VLA 예측대로 움직인다**
7. 종료: **`p`** 일시정지 → **`k`** 제어루프 정지

## 8-6. 환경 — 학습 조건과 맞출 것

| 항목 | 학습 데이터 | 실기에서 |
|---|---|---|
| 카메라 | head 단독 (`ego_view`), 손목 없음 | 동일하게 head-only |
| fps | 25 | `--action-publish-rate 25` |
| **바닥 흰 줄** | **음성 24개에만 있음** | **음성 조건에서 흰 줄이 있어야 학습 조건과 일치** |
| 프롬프트 | `raise your right arm if you see a banana` | 동일 |

> 음성 에피소드 27개 중 24개(ep55~78)가 **바닥에 흰 줄이 있는 상태**로 수집됐다.
> 모델이 바나나가 아니라 흰 줄을 보고 판별할 가능성이 있으므로, **학습과 같은
> 환경에서 실행**해야 결과가 재현된다. 흰 줄 없이 바나나만 치우면 예상과 다르게
> 동작할 수 있다.

## 8-7. 실기에서 반드시 측정할 것

**① 추론 지연** — chunk 도착마다 자동 출력된다.

```
New action chunk (prompt: "...", latency: 0.132s)
```

`지연 × 25Hz` 가 chunk 시작 인덱스가 된다.

| 값 | 조치 |
|---|---|
| 1~2 프레임 이내 | 무시. 현재 평가가 그대로 유효 |
| **4 프레임 이상** | 오프라인 평가에 지연 보상을 반영해 재평가 |

**② 블렌딩 프레임 수** — `--chunk-blend-frames 3` 이 적절한지.

- 동작이 굼뜨면 `2` 로 줄인다
- **주의**: MuJoCo 실측에서 블렌딩 3프레임이 몸통 기울기를 18.2° → 26.9° 로 **늘렸다.** 넘어지진 않았지만 흔들림이 커졌으므로 실기에서 자세를 잘 볼 것
- 이상하면 `0` 으로 즉시 원복

**③ 동작 크기** — 시뮬에서 모델은 GT 대비 팔을 **20~37% 덜 들었다**(163° → 103~133°). 실기에서도 같은 경향인지 확인.

**④ 조건부 판별** — 바나나 있음/없음에서 실제로 갈리는지. **이것만은 실기에서만 확인 가능하다.**

## 8-8. 안전

- MuJoCo 폐루프에서 **넘어짐 0, 관절한계 위반 0, 토크포화 0.2%** 를 확인했다(STEP 7). 다만 그건 덤프된 토큰 재생이고, 실기는 매 순간 이미지를 보고 토큰을 새로 만든다 — **예상 못 한 이미지에서 다른 토큰이 나올 수 있다.**
- 첫 구동은 **사람이 붙잡을 수 있는 상태**로, `p` 로 언제든 일시정지할 수 있게 준비.
- `--initial-pose-blend-duration 1.0` (기본) 유지. `0` 은 초기 자세로 순간 이동해 위험하다.

---

## 미반영 항목 (의도적)

| 항목 | 상태 | 언제 |
|---|---|---|
| chunk 전환 블렌딩 | 코드에 구현됨(`--chunk-blend-frames 3`), **평가에는 미반영** | 실기에서 프레임 수 확정 후 |
| 추론 지연 보상 | 미반영 | 실기 첫 구동 시 `latency: ...s` 실측 후 |
| 바나나 판별 최종 검증 | 불가 | **실기에서만** — 시뮬 렌더링은 실사와 시각 격차가 커서 무의미 |

## 실기 배포 시 필수 설정

```bash
python gear_sonic/scripts/run_vla_inference.py \
    --action-publish-rate 25 \      # ★ 기본값 50이면 동작이 2배 빨라진다
    --chunk-blend-frames 3 \        # 경계 점프 완화 (0 = 기존 동작)
    ...
```
