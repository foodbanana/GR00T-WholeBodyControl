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

## STEP 7 — MuJoCo sim2sim 폐루프 (NVIDIA 구현) — ⏭️ **생략 (2026-08-07)**

> **박사님 판단으로 STEP 7 은 건너뛰고 STEP 8 실기로 바로 간다.**
> 근거: 시뮬 렌더링이 실사와 시각 격차가 커서, 이 과제의 핵심 질문인
> "바나나를 보고 판별하는가"를 sim 에서 확인해봐야 현실과 다르다.
> (아래 "미반영 항목" 표의 *바나나 판별 최종 검증* 항목과 같은 이유다.)
>
> **다만 sim2sim 이 걸러주던 것 하나가 빠진다 — "넘어지지 않는가".**
> 그 몫은 STEP 8 첫 구동에서 **사람이 붙잡을 수 있는 상태 + `p` 즉시 정지 대기**로
> 대신한다. [8-8 안전](#8-8-안전) 을 반드시 지킬 것.
>
> 아래 절차는 나중에 필요해질 때를 위해 남겨둔다.

**NVIDIA 개발자들이 만든 `run_sim_loop.py` 를 쓴다.** 이것이 실기와 같은 구조의
진짜 폐루프다 — MuJoCo가 카메라를 렌더링해 ZMQ로 발행하고, VLA가 그 영상을 보고
매 순간 토큰을 새로 만든다.

| | run_sim_loop (NVIDIA) |
|---|---|
| 시각 입력 | ✅ MuJoCo 카메라 렌더 → ZMQ 발행 (`image_publish_utils.py`) |
| VLA | ✅ 루프 안에 있음 — 매번 새 이미지를 보고 추론 |
| 손 | ✅ `with_hands=True` 기본 — Dex3-1 포함 |
| 실기와의 차이 | 로봇 하드웨어 대신 시뮬레이터 |

### 7-0. 설치 (로봇 없이 미리 가능)

```bash
bash install_scripts/install_mujoco_sim.sh    # .venv_sim 생성
bash install_scripts/install_inference.sh     # .venv_inference 생성
```

### 7-1. 먼저 sim2sim 기본 동작 익히기 (VLA 없이)

VLA를 붙이기 전에 시뮬레이터+컨트롤러만으로 조작에 익숙해질 것. 공식 문서
[quickstart](source/getting_started/quickstart.md) 의 절차다.

```bash
# 터미널 1 — MuJoCo 시뮬레이터
source .venv_sim/bin/activate
python gear_sonic/scripts/run_sim_loop.py

# 터미널 2 — C++ 컨트롤러 (gear_sonic_deploy/ 에서)
bash deploy.sh sim
```

조작: 터미널 2에서 `]` 로 정책 시작 → MuJoCo 창 클릭 후 `9` 로 로봇을 바닥에 내림
→ 터미널 2에서 `T` 로 기준 모션 재생 → `O` 로 정지·종료(비상정지).

**[확인]** 로봇이 넘어지지 않고 서 있어야 한다. 여기가 안 되면 다음으로 가지 말 것.

### 7-2. VLA 를 붙인 폐루프

```bash
python gear_sonic/scripts/launch_inference.py --sim \
    --prompt "raise your right arm if you see a banana"
```

tmux 를 쓰지 않는다면 [STEP 8](#step-8--실기-배포) 의 수동 터미널 절차에서
`--sim` 에 해당하는 부분만 바꿔 쓴다. 키 입력은
`gear_sonic/scripts/keyboard_publisher.py` 로 대신할 수 있다.

**[확인 항목]**
- 넘어지지 않는가
- **바나나가 시야에 있을 때만 오른팔을 드는가** — STEP 6에서 답을 못 낸
  "정지 상태에서 바나나를 보여주면 실제로 팔이 올라가는가"가 여기서 갈린다
- `--action-publish-rate 25` 를 쓰고 있는가 (기본 50이면 동작이 2배 빨라진다)

### 체크포인트 지정

`--model-path` 로 STEP 1~5 에서 고른 체크포인트를 준다. 서버에 있으므로 로컬로
복사하거나 PolicyServer 를 서버에서 띄우고 포트 포워딩한다(8-1 참조).

---

# STEP 8 — 실기 배포

공식 가이드: <https://nvlabs.github.io/GR00T-WholeBodyControl/tutorials/vla_inference.html>
아래는 **우리 환경에 맞춘 차이점 위주**로, 공식 문서 그대로 하면 안 되는 부분을 표시했다.

## 8-0. 사전 조건

| 항목 | 상태 (2026-08-07 확인) | 조치 |
|---|---|---|
| C++ deploy 바이너리 | ✅ **빌드됨** — `gear_sonic_deploy/target/release/g1_deploy_onnx_ref` (2026-07-23) | 없음 |
| **`.venv_inference`** | ✅ **설치됨** — `gr00t` import OK, torch 2.9.0+cu128, `cuda.is_available()=True` | 없음 |
| PolicyServer 구동 위치 | ✅ **안 B (KIST 서버 원격) 확정** — 2026-08-07 변경 | 8-1 참조 |
| 체크포인트 | ck2000 / ck8000 / ck18000 세 개를 서버에서 CLI 로 갈아끼운다 | 8-2 참조 |
| SSH 터널 | ✅ **연결됨** — `localhost:5551 → kist-5090:5550` | 8-1 참조 |
| 카메라 서버 | 로봇 전원 필요 | 로봇 온보드에서 docker 실행 |
| 로봇 네트워크 | 미도달(전원 off, 2026-08-07 ping 무응답) | `ping 192.168.123.164` |

위 4개는 **로봇 없이 미리** 끝낼 수 있다. 로봇 전원이 올라오면 남는 건 카메라 서버뿐이다.

## 8-1. PolicyServer 를 어디서 돌릴 것인가 — ✅ **안 B (원격) 확정**

| 안 | 장점 | 문제 |
|---|---|---|
| A. DGX Spark(로컬) | 네트워크 지연 없음 | 체크포인트마다 6.5GB 를 내려받아야 하고, GB10 한 장을 추론이 점유한다 |
| **B. KIST 서버(원격)** ✅ | 체크포인트가 이미 서버에 다 있어 **CLI 로 즉시 교체**. GPU 4장 | 네트워크 지연 — **아래에서 해결됨** |

**B 로 간다.** ck2000/ck8000/ck18000 을 번갈아 비교하는 것이 목적이라,
매번 6.5GB 를 내려받는 대신 서버에서 `--ckpt` 만 바꾸는 쪽이 맞다.

### 서버에서 띄우기 — NVIDIA 공식 명령어 + 한 줄

**래퍼 스크립트를 쓰지 않는다.** `run_gr00t_server.py` 의 `--model-path` 가 이미
체크포인트를 고르는 CLI 인자다. 공식 문서 명령어를 그대로 쓰되,
**이 서버에서는 앞에 `source ~/groot_env.sh` 가 반드시 필요하다** (이유는 바로 아래).

```bash
ssh kist-5090
tmux new -s policy                      # SSH 를 닫아도 살아 있게

source ~/groot_env.sh                   # ★ 없으면 아래에서 401 로 죽는다
cd ~/Isaac-GR00T
uv run python gr00t/eval/run_gr00t_server.py \
    --model-path ~/groot_output/rab-v2b-20260806/checkpoint-2000 \
    --embodiment-tag UNITREE_G1_SONIC \
    --device cuda:0 \
    --port 5550
```

체크포인트 교체는 `Ctrl+C` 로 끄고 `--model-path` 의 숫자만 바꿔 다시 띄운다
(같은 포트를 두 번 bind 할 수 없다). 붙었다 떨어지기: `Ctrl+b d` / `tmux a -t policy`.

가능한 체크포인트 목록:

```bash
ls -d ~/groot_output/rab-v2b-20260806/checkpoint-* | sed 's#.*checkpoint-##' | sort -n
```

### ★ 함정 — `source ~/groot_env.sh` 없이 공식 명령어만 치면 401 로 죽는다

```
RuntimeError: Cannot download the VLM backbone 'nvidia/Cosmos-Reason2-2B',
which is a gated Hugging Face repo.
401 Client Error ... Access to model nvidia/Cosmos-Reason2-2B is restricted.
```

GR00T 체크포인트는 **VLM 백본을 항상 별도로 로드**한다. 이 서버는 백본과 HF 토큰이
`~/hf_cache` 에 있고, 그 경로는 `groot_env.sh` 의 `export HF_HOME=/home/ltw1203/hf_cache`
로만 잡힌다.

| 경로 | 내용 |
|---|---|
| `~/hf_cache/hub/` | `models--nvidia--Cosmos-Reason2-2B`, `models--nvidia--GR00T-N1.7-3B` + `token` ✅ |
| `~/.cache/huggingface/` (HF 기본값) | **비어 있음, 토큰 없음** ❌ |

`HF_HOME` 을 안 잡으면 HF 기본 경로를 보고 → 캐시 없음 → 다운로드 시도 →
게이트 걸린 repo 라 401. 체크포인트 경로가 맞아도 죽는다.

> `--model-path` 가 로컬 경로라 HF 를 안 탈 것 같지만 **아니다.** 백본만은 항상 HF 를 탄다.
> 2026-08-07 실제로 확인함.

### 로그를 남기고 싶으면

```bash
uv run python gr00t/eval/run_gr00t_server.py ... 2>&1 | tee ~/groot_output/policy_server_5550.log
```

### SSH 터널

로컬 5550 은 이미 다른 서버가 쓸 수 있으므로 **5551 로 뺀다**:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
    -L 5551:localhost:5550 kist-5090
```

클라이언트는 `--host localhost --port 5551` 로 붙는다.

### 원격 지연 — 측정값과 그 결과 바뀐 것

**전송량이 곧 지연이다.** 관측치는 msgpack 으로 **압축 없이 raw uint8** 로
넘어가므로, 이미지 해상도가 왕복 시간을 그대로 결정한다.
터널 너머 실측(2026-08-07, ck2000):

| ego_view 해상도 | 전송량/회 | 2.5Hz 환산 | **왕복 시간** |
|---|---|---|---|
| 1920x1080 (기존 기본값) | 6.22 MB | 15.6 MB/s (124 Mbps) | **647 ms** ❌ |
| 960x540 | 1.56 MB | 3.9 MB/s (31 Mbps) | 243 ms |
| **640x480** ✅ | 0.92 MB | 2.3 MB/s (18 Mbps) | **192 ms** |

2.5Hz 예산이 400ms 인데 **원본 해상도는 그것만으로 예산을 넘긴다.**

`run_vla_inference.py` 는 원래 카메라를 `decode_reduce_factor=1`(1080p 원본)로
읽어 그대로 보내고 있었다. 그래서 **보내기 전에 640x480 으로 줄이도록 고쳤다**:

```
--camera-decode-reduce 2      # 1080p -> 960x540 (libjpeg scaled decode)
--camera-image-size 640x480   # -> 640x480 (INTER_AREA)
```

이 값이 새 기본값이고, `launch_inference.py` 에도 같은 인자를 뚫어놨다.

이건 속도만의 문제가 아니다. **데이터 수집 때 `run_data_exporter.py` 가 쓴
전처리 경로와 정확히 같다** (reduce 2 → `cv2.resize(..., INTER_AREA)` → 640x480,
`meta/info.json` 의 `observation.images.ego_view` = `[480, 640, 3]`).
원본을 그대로 보내면 학습 때와 다른 리샘플링을 거친 이미지를 모델에 주게 된다.

## 8-2. 체크포인트 — ck2000 / ck8000 / ck18000 비교

세 개를 **실기에서 직접 비교**한다. 서버에 다 있으므로 내려받을 것은 없다.
`--model-path` 끝의 숫자만 바꾸면 된다 (서버 재기동 필요).

```bash
CK=~/groot_output/rab-v2b-20260806
--model-path $CK/checkpoint-2000     # 초기 학습
--model-path $CK/checkpoint-8000     # 중간
--model-path $CK/checkpoint-18000    # 관절공간 최저
```

**사전 기대치는 ck18000 이다.**
[`eval_results/decoded_openloop_v2_h10/README.md`](../eval_results/decoded_openloop_v2_h10/README.md)
결론 2 에서 **val/loss 와 관절공간 성능의 상관이 −0.605 로 방향이 반대**임이
확인됐다. val/loss 최저인 ck4000 은 관절공간에서 29.93° 로 거의 최악이고,
ck18000 은 **양성 8ep 평균 23.02° 로 관절공간 최저**다.
ck2000/ck8000 은 그 곡선의 앞쪽이라, 실기에서 정말 단조 개선인지 보는 용도다.

> 로컬 `~/models/` 에도 ck2000/ck18000 사본이 있다(이전 세션 작업).
> `ck8000` 은 **복사가 중간에 끊겨 shard 2 가 없다** — 로컬로 돌릴 생각이면
> 먼저 다시 받아야 한다. 원격으로 가는 한 상관없다.

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

### Terminal 2 — PolicyServer (KIST 서버 원격) + SSH 터널

**2-a. 서버에서 띄운다** (체크포인트는 `--model-path` 로 고른다):

```bash
ssh kist-5090
tmux new -s policy
source ~/groot_env.sh                   # ★ 빠뜨리면 401 로 죽는다 (8-1 참조)
cd ~/Isaac-GR00T
uv run python gr00t/eval/run_gr00t_server.py \
    --model-path ~/groot_output/rab-v2b-20260806/checkpoint-2000 \
    --embodiment-tag UNITREE_G1_SONIC \
    --device cuda:0 \
    --port 5550
```

`Loading checkpoint shards: 100%` 가 뜨고 포트가 열리면 준비된 것이다
(로드에 약 1분). `Ctrl+b d` 로 빠져나온다.

**2-b. 로컬에서 터널을 연다** (5550 은 이미 쓰일 수 있어 **5551** 로):

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
    -L 5551:localhost:5550 kist-5090
```

> **`--port 5550` 을 빠뜨리면 안 된다.** `run_gr00t_server.py` 의 기본 포트는
> **5555** 로, 카메라 서버와 같은 번호다. 반면 `run_vla_inference.py` 의
> `--port` 기본값은 **5550** 이라, 서버만 기본값으로 띄우면 클라이언트가
> 영영 붙지 못한다 (에러 없이 그냥 조용히 멈춘 것처럼 보인다).
> 공식 문서 예시에도 `--port 5550` 이 명시돼 있다 — 그대로 따르면 된다.

원격이므로 Terminal 5 는 `--host localhost --port 5551` 로 붙는다.

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
    --host localhost --port 5551 \
    --embodiment-tag unitree_g1_sonic \
    --prompt "raise your right arm if you see a banana" \
    --camera-host 192.168.123.164 --camera-port 5555 \
    --action-publish-rate 25 \
    --chunk-blend-frames 0 \
    --camera-image-size 640x480 --camera-decode-reduce 2
```

| 인자 | 공식 기본값 | **우리 값** | 이유 |
|---|---|---|---|
| `--action-publish-rate` | 50 | **25** | 모델이 **25fps** 데이터로 학습됐다. `delta_indices=range(40)` 이 데이터셋 프레임 40개 연속이라 리샘플링이 없어, 50이면 **동작이 2배 속도로 재생**된다 |
| `--chunk-blend-frames` | (없음) | **첫 구동 0 → 이후 3** | 우리가 추가한 옵션. chunk 경계에서 관절목표가 평균 20°(최대 204°) 튀는 것을 완화하지만 **하드웨어 미검증**이다. 검증된 동작(`0`)으로 기준을 먼저 잡고, 경계 점프가 실제로 보이면 `3` 으로 올린다 |
| `--prompt` | `demo` | 학습과 **동일 문장** | 다르면 모델이 이미지 대신 프롬프트로 판별할 여지가 생긴다 |
| `--camera-image-size` | `640x480` | 기본값 그대로 | 8-1 참조. 원본 1080p 를 보내면 왕복이 647ms 로 2.5Hz 예산(400ms)을 넘고, 학습 때와 다른 리샘플링을 거친다. **원격 PolicyServer 에서는 특히 건드리지 말 것** |
| `--camera-decode-reduce` | `2` | 기본값 그대로 | 1080p 를 libjpeg 축소 디코드로 960x540 까지 싸게 내린다. 데이터 수집 때와 같은 경로 |

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
- **미검증**: 블렌딩은 하드웨어에서 확인한 적이 없어 기본값이 `0`(꺼짐)이다. 켤 때는 자세 흔들림을 보면서 늘릴 것
- 이상하면 `0` 으로 즉시 원복

**③ 동작 크기** — open-loop 평가에서 모델의 어깨 pitch 진폭은 GT 명령의 **95~99%** 였다(ck18000 기준). 다만 **로봇은 명령의 82%까지만 도달한다**(PD 정상상태 오차, 어깨 pitch 평균 −14.9°) — 데이터 수집 때와 같은 현상이므로 정상이다.

**④ 조건부 판별** — 바나나 있음/없음에서 실제로 갈리는지. **이것만은 실기에서만 확인 가능하다.**

## 8-8. 안전

**STEP 7 sim2sim 을 생략했으므로 이 절이 유일한 안전망이다.** 시뮬이 걸러주던
"넘어지는가"를 실기 첫 구동에서 사람이 직접 받아내야 한다.

- 첫 구동은 **사람이 붙잡을 수 있는 상태**로, `p` 로 언제든 일시정지할 수 있게 준비.
  - 키보드 publisher(Terminal 4)에 **손을 올려둔 채로** 시작할 것.
  - 이상하면 `p`(추론 정지) → 그래도 안 되면 `k`(제어루프 정지).
- **첫 시도는 음성 조건(바나나 없음)으로 한다.** 정답이 "가만히 있기"라
  모델이 크게 움직이면 그 자체가 이상 신호다. 양성 조건은 그다음.
- `--initial-pose-blend-duration 1.0` (기본) 유지. `0` 은 초기 자세로 순간 이동해 위험하다.
- `--chunk-blend-frames` 는 **하드웨어 미검증**이다. 첫 구동은 `0`(꺼짐)으로 시작해
  기준 동작을 본 뒤, 경계 점프가 실제로 보이면 그때 `3` 으로 올린다.

### 초기 자세 토큰 — 데모 시작 자세와 다르다 (알고 가는 것)

`i` 를 누르면 가는 `LATENT_INITIAL_MOTION_TOKEN`
([`gear_sonic/utils/inference/initial_poses.py`](../gear_sonic/utils/inference/initial_poses.py))
은 NVIDIA 기본 standing 토큰이다. v2 데이터셋 79개 에피소드의 frame-0 토큰과
비교하면 (2026-08-07 측정):

| 항목 | 값 |
|---|---|
| `‖INITIAL_TOKEN‖` | 1.118 |
| `‖평균 frame-0 토큰‖` | 1.282 |
| 에피소드 간 자체 편차 | 평균 0.482 / 최대 1.080 |
| **dist(frame-0, INITIAL)** | 최소 0.988 / **평균 1.210** / 최대 1.831 |
| cosine(평균 frame-0, INITIAL) | **0.579** |

즉 **데모 시작 분포에서 에피소드 자체 편차의 약 2.3배 떨어져 있고 방향도 다르다.**
정책이 한 번도 시작점으로 본 적 없는 자세에서 추론이 시작되므로,
**첫 action chunk 가 튈 수 있다.**

**현재는 값을 유지하기로 했다** — 알려진 안전 standing 자세라는 점이 첫 구동에서는
더 중요하다는 판단. 대신 이렇게 대응한다:

- 첫 chunk 를 특히 주의해서 본다 (`p` 에 손 올려둔 채로)
- 튀면 `--initial-pose-blend-duration` 을 2.0 으로 올려 완충
- 그래도 안 되면 데모 시작 토큰으로 교체한다. medoid 는 `episode_000044` 의
  frame-0 (평균과의 거리 0.222 로 79개 중 가장 대표적)

---

## 미반영 항목 (의도적)

| 항목 | 상태 | 언제 |
|---|---|---|
| chunk 전환 블렌딩 | 코드에 구현됨(`--chunk-blend-frames 3`), **평가에는 미반영** | 실기에서 프레임 수 확정 후 |
| 추론 지연 보상 | 미반영 | 실기 첫 구동 시 `latency: ...s` 실측 후 |
| 바나나 판별 최종 검증 | 불가 | **실기에서만** — 시뮬 렌더링은 실사와 시각 격차가 커서 무의미 |

## 실기 배포 시 필수 설정

```bash
# PolicyServer — KIST 서버 (Terminal 2-a)
ssh kist-5090 && tmux new -s policy
source ~/groot_env.sh           # ★ 없으면 gated repo 401 로 죽는다 (HF_HOME)
cd ~/Isaac-GR00T
uv run python gr00t/eval/run_gr00t_server.py \
    --model-path ~/groot_output/rab-v2b-20260806/checkpoint-2000 \
    --embodiment-tag UNITREE_G1_SONIC --device cuda:0 --port 5550
#                                ^^^^ 여기만 바꿔 체크포인트 교체
#   ★ --port 를 빼면 기본값 5555 (카메라 서버와 충돌) 라 클라이언트가 못 붙는다

# SSH 터널 — 로컬 (Terminal 2-b)
ssh -N -L 5551:localhost:5550 kist-5090

# VLA Inference (Terminal 5)
python gear_sonic/scripts/run_vla_inference.py \
    --host localhost --port 5551 \  # ★ 원격 PolicyServer 로 가는 터널
    --action-publish-rate 25 \      # ★ 기본값 50이면 동작이 2배 빨라진다
    --chunk-blend-frames 0 \        # 첫 구동은 검증된 0. 경계 점프 보이면 3
    --camera-image-size 640x480 \   # ★ 원본 1080p 면 왕복 647ms 로 예산 초과
    ...
```
