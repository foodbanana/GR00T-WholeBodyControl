# KIST 서버(cluster101, RTX 5090 x4) VLA 파인튜닝 런북

작성일: 2026-07-29
대상 데이터셋: `outputs/raise_arm_banana_merged` (55 ep / 11,208 frame / 25fps / ego_view 1대 / 128MB)
태스크: `"raise your right arm if you see a banana"`

전제:
- 서버에 Isaac-GR00T 환경 **없음** (처음부터 세팅)
- 데이터 전송 출발지: **DGX Spark** (<GATEWAY_IP>:4648 도달 확인됨)
- 서버 외부망 가용 여부: **미확인** → Phase 1에서 분기
- wandb 사용

---

## 서버 구조 요약

```
DGX Spark (여기)
   └─ ssh ─▶ <GATEWAY_IP>:4648   데이터서버 cluster100 (NIS+NFS 마스터, /home 제공)
                └─ ssh ─▶ <GPU_SERVER_IP>:4648   cluster101 = RTX 5090 x4  ← 학습 실행
```

- `/home` 은 데이터서버에서 NFS 마운트 → cluster100과 cluster101이 **같은 홈을 공유**.
  대용량 체크포인트/venv를 여기 두면 느리고 공용 쿼터를 먹는다.
- `/data` 는 cluster101 **로컬 디스크**.
  - `/data/data1` (4TB) — `drwxr-xr-x user user` → **<USER> 쓰기 불가**
  - `/data/data2` (8TB) — `drwxrwxrwx root root` → **쓰기 가능** ✅

→ **작업 루트는 `/data/data2/<USER>/gr00t`**

```
/data/data2/<USER>/gr00t/
├── Isaac-GR00T/                     # 레포 + .venv
├── dataset/raise_arm_banana_merged/ # 학습 데이터
├── hf_cache/                        # HF_HOME
└── output/                          # 체크포인트
```

---

## Phase 0 — 로컬(DGX Spark) 준비

### 0-1. SSH config 등록 (2단 점프를 한 줄로)

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cat >> ~/.ssh/config <<'EOF'

Host kist-gw
    HostName <GATEWAY_IP>
    Port 4648
    User <USER>
    ServerAliveInterval 30

Host kist-5090
    HostName <GPU_SERVER_IP>
    Port 4648
    User <USER>
    ProxyJump kist-gw
    ServerAliveInterval 30
EOF
chmod 600 ~/.ssh/config
```

### 0-2. 키 등록 (비밀번호 반복 입력 제거)

```bash
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id kist-gw       # 비밀번호 1회
ssh-copy-id kist-5090     # 비밀번호 1회 (홈이 NFS 공유라 보통 이미 통과됨)
ssh kist-5090 'hostname; whoami'   # 무암호로 RTX5090 나오면 성공
```

### 0-3. 데이터셋 최종 점검

```bash
cd /home/edgexpert00/GR00T-WholeBodyControl/outputs/raise_arm_banana_merged
python3 - <<'EOF'
import json, pathlib, collections
info = json.loads(pathlib.Path("meta/info.json").read_text())
eps  = [json.loads(l) for l in open("meta/episodes.jsonl")]
npq  = len(list(pathlib.Path("data/chunk-000").glob("*.parquet")))
vids = {d.name: len(list(d.glob("*.mp4"))) for d in pathlib.Path("videos/chunk-000").iterdir()}
print("total_episodes:", info["total_episodes"], "| episodes.jsonl:", len(eps), "| parquet:", npq)
print("total_videos  :", info["total_videos"], "| mp4:", vids)
print("total_frames  :", info["total_frames"], "| sum(len):", sum(e["length"] for e in eps))
print("splits        :", info["splits"], "  <- 반드시 0:%d" % info["total_episodes"])
print("fps           :", info["fps"])
assert pathlib.Path("meta/modality.json").exists(), "modality.json 없음!"
EOF
```

모든 수치가 55 / 55 / 55, `splits == {"train": "0:55"}` 여야 한다.

> ⚠️ `gear_sonic/scripts/process_dataset.py` 는 `total_episodes`/`total_frames`만 갱신하고
> `total_videos`/`splits`는 원본 값을 그대로 둔다. merge/process를 다시 돌리면 이 점검을 재실행할 것.

---

## Phase 1 — 서버 접속 및 환경 점검

```bash
ssh kist-5090
```

접속 후 아래를 **한 번에** 실행:

```bash
echo "===== HOST ====="; hostname; whoami; id
echo "===== GPU ====="; nvidia-smi
echo "===== GPU 점유자 ====="; nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv
echo "===== DISK ====="; df -h /home /data/data1 /data/data2
echo "===== /data 권한 ====="; ls -ld /data/data1 /data/data2
echo "===== CPU/RAM ====="; nproc; free -h
echo "===== 외부망 ====="
for u in https://huggingface.co https://github.com https://api.wandb.ai https://astral.sh; do
  code=$(timeout 10 curl -sI -o /dev/null -w '%{http_code}' "$u" 2>/dev/null || echo TIMEOUT)
  echo "  $u -> $code"
done
echo "===== 기존 도구 ====="; which uv git tmux python3 nvcc; python3 -V
```

판정:
- 외부망 4개 모두 `200`/`301` → **경로 A** (서버에서 직접 다운로드)
- 하나라도 `TIMEOUT`/`000` → **경로 B** (DGX Spark에서 통째로 전송)
- `nvidia-smi`에 남의 프로세스가 GPU를 물고 있으면 사용할 GPU 수를 조정할 것

---

## Phase 2 — 작업 폴더 생성

```bash
export WORK=/data/data2/<USER>/gr00t
mkdir -p $WORK/{dataset,hf_cache,output,logs}
ls -ld $WORK $WORK/*

# 셸 재접속 후에도 유지되도록
cat >> ~/.bashrc <<'EOF'

# ---- GR00T finetune ----
export WORK=/data/data2/<USER>/gr00t
export HF_HOME=$WORK/hf_cache
export HF_HUB_DISABLE_XET=1          # Xet 백엔드 속도 저하 회피 (데스크탑에서 확인된 이슈)
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
EOF
source ~/.bashrc
echo "WORK=$WORK  HF_HOME=$HF_HOME"
```

---

## Phase 3 — 데이터셋 전송 (DGX Spark에서 실행)

**DGX Spark 터미널**에서:

```bash
rsync -avhP --stats \
  /home/edgexpert00/GR00T-WholeBodyControl/outputs/raise_arm_banana_merged \
  kist-5090:/data/data2/<USER>/gr00t/dataset/
```

검증 (서버에서):

```bash
du -sh $WORK/dataset/raise_arm_banana_merged
ls $WORK/dataset/raise_arm_banana_merged/data/chunk-000 | wc -l    # 55
ls $WORK/dataset/raise_arm_banana_merged/videos/chunk-000/observation.images.ego_view | wc -l  # 55
cat $WORK/dataset/raise_arm_banana_merged/meta/info.json | head -20
```

---

## Phase 4 — Isaac-GR00T 설치

### 4-1. uv 설치

경로 A(외부망 O):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv --version
```

경로 B(외부망 X) — DGX Spark에서 바이너리 전송:
```bash
# DGX Spark
scp $(which uv) kist-5090:~/.local/bin/uv     # aarch64/x86 아키텍처 불일치 주의
# 아키텍처 다르면 https://github.com/astral-sh/uv/releases 에서
# uv-x86_64-unknown-linux-gnu.tar.gz 받아 scp
```

### 4-2. 레포 클론

경로 A:
```bash
cd $WORK
git clone https://github.com/NVIDIA/Isaac-GR00T.git
cd Isaac-GR00T && git log -1 --oneline
```

경로 B — DGX Spark에서 클론 후 전송:
```bash
# DGX Spark
git clone https://github.com/NVIDIA/Isaac-GR00T.git /tmp/Isaac-GR00T
rsync -avhP /tmp/Isaac-GR00T kist-5090:/data/data2/<USER>/gr00t/
```

### 4-3. 의존성 설치

```bash
cd $WORK/Isaac-GR00T
export UV_CACHE_DIR=$WORK/.uv_cache      # 홈(NFS) 대신 로컬 디스크 사용
uv sync --all-extras 2>&1 | tee $WORK/logs/uv_sync.log
```

### 4-4. ⚠️ RTX 5090 (Blackwell, sm_120) 커널 검증 — **반드시 할 것**

RTX 5090은 compute capability **12.0**이다. PyTorch가 cu124 이하 빌드면
`torch.cuda.is_available()`은 True를 반환하면서도 실제 커널 실행에서
`no kernel image is available for execution on the device`로 죽는다.
**단순 is_available() 체크로는 못 잡는다. 실제 연산을 돌려야 한다.**

```bash
cd $WORK/Isaac-GR00T
uv run python - <<'EOF'
import torch
print("torch      :", torch.__version__)
print("cuda(build):", torch.version.cuda)
print("available  :", torch.cuda.is_available())
print("gpu count  :", torch.cuda.device_count())
print("arch list  :", torch.cuda.get_arch_list())
for i in range(torch.cuda.device_count()):
    p = torch.cuda.get_device_properties(i)
    print(f"  GPU{i}: {p.name}  sm_{p.major}{p.minor}  {p.total_memory/1e9:.1f}GB")
# 실제 커널 실행 테스트 (여기서 죽으면 torch 재설치 필요)
x = torch.randn(4096, 4096, device="cuda:0", dtype=torch.bfloat16)
y = (x @ x).float().sum().item()
print("bf16 matmul OK:", y)
print("NCCL       :", torch.distributed.is_nccl_available())
EOF
```

`arch_list`에 `sm_120`이 없거나 matmul이 실패하면:

```bash
cd $WORK/Isaac-GR00T
uv pip install --upgrade --force-reinstall \
  torch torchvision --index-url https://download.pytorch.org/whl/cu128
# 재검증 후 flash-attn 계열이 깨지면 해당 패키지도 재빌드/재설치 필요
```

### 4-5. 멀티 GPU 통신 확인

```bash
uv run python - <<'EOF'
import torch
n = torch.cuda.device_count()
assert n >= 2, f"GPU {n}개뿐"
x = torch.randn(2048, 2048, device="cuda:0")
for i in range(1, n):
    x = x.to(f"cuda:{i}")
x = x.to("cuda:0")
print(f"{n}-GPU P2P transfer OK")
EOF
```

---

## Phase 5 — 모델 가중치 확보

### 5-1. HuggingFace 인증 (Cosmos-Reason2-2B는 gated)

```bash
cd $WORK/Isaac-GR00T
uv run hf auth login          # 구버전이면: uv run huggingface-cli login
uv run hf auth whoami
```

### 5-2. 다운로드

경로 A:
```bash
cd $WORK/Isaac-GR00T
export HF_HOME=$WORK/hf_cache
export HF_HUB_DISABLE_XET=1              # ← 이거 없으면 KB/s 단위로 떨어짐
export HF_HUB_DOWNLOAD_TIMEOUT=60
uv run hf download nvidia/GR00T-N1.7-3B      2>&1 | tail -5
uv run hf download nvidia/Cosmos-Reason2-2B  2>&1 | tail -5
du -sh $WORK/hf_cache
```

경로 B — 4090 데스크탑에 이미 받아둔 캐시를 그대로 전송:
```bash
# 데스크탑(taeung)에서
rsync -avhP ~/.cache/huggingface/hub/models--nvidia--GR00T-N1.7-3B \
            ~/.cache/huggingface/hub/models--nvidia--Cosmos-Reason2-2B \
  kist-5090:/data/data2/<USER>/gr00t/hf_cache/hub/
```

### 5-3. wandb 로그인

```bash
cd $WORK/Isaac-GR00T
uv run wandb login          # https://wandb.ai/authorize 에서 API key 복사
```

---

## Phase 6 — 서버 스모크 테스트 (본 학습 전 필수)

데스크탑에서 통과했더라도 **서버는 GPU 아키텍처·드라이버·경로가 전부 다르다.** 두 단계로 확인한다.

### 6-1. 단일 GPU 20 step

```bash
cd $WORK/Isaac-GR00T
export CUDA_VISIBLE_DEVICES=0
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export HF_HOME=$WORK/hf_cache
export HF_HUB_DISABLE_XET=1

uv run python gr00t/experiment/launch_finetune.py \
    --base-model-path nvidia/GR00T-N1.7-3B \
    --dataset-path $WORK/dataset/raise_arm_banana_merged \
    --embodiment-tag UNITREE_G1_SONIC \
    --modality-config-path gr00t/configs/data/embodiment_configs.py \
    --output-dir $WORK/output/smoke_1gpu \
    --num-gpus 1 \
    --global-batch-size 1 \
    --max-steps 20 \
    --save-steps 20 \
    --save-total-limit 1 \
    --save-only-model \
    --dataloader-num-workers 0 \
    --no-use-wandb 2>&1 | tee $WORK/logs/smoke_1gpu.log
```

### 6-2. 4 GPU 분산 50 step (NCCL·샤딩 검증)

```bash
cd $WORK/Isaac-GR00T
unset CUDA_VISIBLE_DEVICES

uv run python gr00t/experiment/launch_finetune.py \
    --base-model-path nvidia/GR00T-N1.7-3B \
    --dataset-path $WORK/dataset/raise_arm_banana_merged \
    --embodiment-tag UNITREE_G1_SONIC \
    --modality-config-path gr00t/configs/data/embodiment_configs.py \
    --output-dir $WORK/output/smoke_4gpu \
    --num-gpus 4 \
    --global-batch-size 16 \
    --max-steps 50 \
    --save-steps 50 \
    --save-total-limit 1 \
    --save-only-model \
    --dataloader-num-workers 4 \
    --no-use-wandb 2>&1 | tee $WORK/logs/smoke_4gpu.log
```

**돌아가는 동안 다른 터미널에서 VRAM 확인:**

```bash
ssh kist-5090 'watch -n2 nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv'
```

- 피크 VRAM이 카드당 28GB 넘으면 → 본 학습에서 `--global-batch-size`를 8로 낮춘다
- 20GB 이하로 여유 있으면 → 32로 올린다

---

## Phase 7 — 본 학습

**반드시 tmux 안에서 실행** (ssh 끊겨도 학습 유지).

```bash
ssh kist-5090
tmux new -s ft
```

tmux 안에서:

```bash
cd $WORK/Isaac-GR00T
unset CUDA_VISIBLE_DEVICES
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export HF_HOME=$WORK/hf_cache
export HF_HUB_DISABLE_XET=1
export WANDB_PROJECT=g1-sonic-raise-arm-banana
export WANDB_NAME=n17-55ep-bs16-10k

uv run python gr00t/experiment/launch_finetune.py \
    --base-model-path nvidia/GR00T-N1.7-3B \
    --dataset-path $WORK/dataset/raise_arm_banana_merged \
    --embodiment-tag UNITREE_G1_SONIC \
    --modality-config-path gr00t/configs/data/embodiment_configs.py \
    --output-dir $WORK/output/raise_arm_banana_n17 \
    --num-gpus 4 \
    --global-batch-size 16 \
    --max-steps 10000 \
    --save-steps 1000 \
    --save-total-limit 10 \
    --color-jitter-params brightness 0.3 contrast 0.4 saturation 0.5 hue 0.08 \
    --dataloader-num-workers 4 \
    --use-wandb 2>&1 | tee $WORK/logs/train_$(date +%Y%m%d_%H%M).log
```

`Ctrl-b d` 로 detach, `tmux attach -t ft` 로 복귀.

### 공식 문서 기본값과 다르게 잡은 이유

| 플래그 | 문서 | 여기 | 이유 |
|---|---|---|---|
| `--max-steps` | 20000 | **10000** | 55 ep / 11,208 frame은 문서 권장(50~100 데모)의 하한. bs16×20k = 320k 샘플 ≈ 29 epoch → 과적합 위험. 1000 step마다 저장해 두고 실기 평가로 최적 체크포인트 선택 |
| `--global-batch-size` | 32 | **16** | 5090은 32GB (A100/H100 80GB 아님). Phase 6-2 VRAM 실측 후 조정 |
| `--save-steps` | 5000 | **1000** | 데이터가 작아 수렴이 빠르다. 촘촘히 남겨 비교 |
| `--save-total-limit` | 5 | **10** | 위와 동일. `df -h /data/data2` 로 용량 확인하며 조정 |

`--save-only-model`은 **넣지 않는다** (중단 시 재개 가능해야 하므로). 다만 체크포인트 하나가
옵티마이저 상태 포함 수십 GB이므로 첫 저장 직후 실제 크기를 확인하고 limit을 조정할 것:

```bash
du -sh $WORK/output/raise_arm_banana_n17/checkpoint-1000
df -h /data/data2
```

---

## Phase 8 — 모니터링

```bash
# 진행 로그
ssh kist-5090 'tail -f /data/data2/<USER>/gr00t/logs/train_*.log'

# GPU
ssh kist-5090 'watch -n5 nvidia-smi'

# 체크포인트
ssh kist-5090 'ls -lh /data/data2/<USER>/gr00t/output/raise_arm_banana_n17/'
```

wandb: `https://wandb.ai/<계정>/g1-sonic-raise-arm-banana`
→ train loss가 꾸준히 감소하다 `--max-steps` 전에 plateau 되어야 정상.
초반 수백 step에서 loss가 평평하거나 NaN이면 즉시 중단하고 데이터/정규화 경로를 의심할 것.

---

## Phase 9 — 다음 단계 (배포)

```bash
# 서버(GPU 머신)에서 PolicyServer
cd $WORK/Isaac-GR00T
uv run python gr00t/eval/run_gr00t_server.py \
    --model-path $WORK/output/raise_arm_banana_n17/checkpoint-10000 \
    --embodiment-tag UNITREE_G1_SONIC \
    --device cuda:0 \
    --port 5550

# 추론 머신(DGX Spark, GR00T-WholeBodyControl 레포)에서
python gear_sonic/scripts/launch_inference.py \
    --policy-host <gpu_machine_ip> --policy-port 5550 \
    --camera-host 192.168.123.164 \
    --prompt "raise your right arm if you see a banana"
```

> 서버(<GATEWAY_IP> 뒤 사설망 <GPU_SERVER_IP>)는 로봇 네트워크에서 직접 안 보인다.
> 실기 추론 시에는 SSH 리버스 터널이 필요하다:
> `ssh -N -L 5550:<GPU_SERVER_IP>:5550 kist-gw` 형태로 5550 포워딩.

---

## 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `no kernel image is available` | torch가 sm_120 미지원 → Phase 4-4의 cu128 재설치 |
| HF 다운로드가 KB/s로 멈춘 듯함 | Xet 백엔드 → `export HF_HUB_DISABLE_XET=1` |
| `401/403` on Cosmos-Reason2-2B | gated repo → `hf auth login` + 모델 페이지에서 라이선스 동의 |
| CUDA OOM | `--global-batch-size` 반감 → 8 → 4. 그래도 안 되면 `--no-tune-diffusion-model` (단, 성능 저하) |
| 학습 중 ssh 끊김 | tmux 미사용. `tmux attach -t ft` 또는 재시작 |
| `/data/data2` full | `--save-total-limit` 축소, 이전 smoke 출력 삭제 |
| dataloader 정지/느림 | 데이터가 NFS 홈에 있는지 확인. 반드시 `/data/data2` 로컬에 둘 것 |
