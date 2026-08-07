# 다음 세션 실행 체크리스트 — KIST 서버 VLA 파인튜닝

상세 설명은 [server_finetune_runbook.md](server_finetune_runbook.md) 참조.
이 파일은 **위에서부터 순서대로 복붙**하면 되는 실행 목록이다.

현재 상태 (2026-07-29 기준):
- ✅ `outputs/raise_arm_banana_merged` 검증 완료 (55 ep / 11,208 frame / 25fps / ego_view 1대 / 128MB)
- ✅ `meta/info.json` 의 `total_videos`/`splits` 수정 완료 (다른 세션에서 처리됨)
- ✅ DGX Spark → <GATEWAY_IP>:4648 TCP 도달 확인
- ⬜ Phase 0부터 시작

각 STEP의 **[검증]** 이 통과해야 다음으로 넘어간다.

---

## STEP 0 — 로컬 데이터셋 재확인 (DGX Spark)

```bash
cd /home/edgexpert00/GR00T-WholeBodyControl/outputs/raise_arm_banana_merged
python3 - <<'EOF'
import json, pathlib
info = json.loads(pathlib.Path("meta/info.json").read_text())
eps  = [json.loads(l) for l in open("meta/episodes.jsonl")]
npq  = len(list(pathlib.Path("data/chunk-000").glob("*.parquet")))
nmp4 = len(list(pathlib.Path("videos/chunk-000/observation.images.ego_view").glob("*.mp4")))
ok = True
def chk(label, got, want):
    global ok
    good = got == want; ok &= good
    print(f"{'OK ' if good else 'FAIL'} {label}: {got} (기대 {want})")
chk("total_episodes", info["total_episodes"], 55)
chk("episodes.jsonl", len(eps), 55)
chk("parquet",        npq, 55)
chk("mp4",            nmp4, 55)
chk("total_videos",   info["total_videos"], 55)
chk("splits",         info["splits"], {"train": "0:55"})
chk("total_frames",   info["total_frames"], sum(e["length"] for e in eps))
chk("fps",            info["fps"], 25)
chk("modality.json",  pathlib.Path("meta/modality.json").exists(), True)
print("\n=== 전체:", "PASS" if ok else "FAIL ===")
EOF
```

**[검증]** 마지막 줄이 `PASS`. 하나라도 FAIL이면 여기서 멈추고 merge를 다시 볼 것.

---

## STEP 1 — SSH 설정 (DGX Spark)

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
grep -q 'Host kist-5090' ~/.ssh/config 2>/dev/null || cat >> ~/.ssh/config <<'EOF'

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

[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id kist-gw       # 서버 비밀번호 입력
ssh-copy-id kist-5090     # 서버 비밀번호 입력
```

**[검증]** 무암호로 `RTX5090` / `<USER>` 이 나와야 한다.

```bash
ssh kist-5090 'hostname; whoami'
```

---

## STEP 2 — 서버 환경 일괄 점검

```bash
ssh kist-5090 'bash -s' <<'EOF'
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
echo "===== 기존 도구 ====="; which uv git tmux python3 nvcc 2>/dev/null; python3 -V
EOF
```

**[검증] / 분기 판단**
- GPU 4장이 보이고, 남이 점유 중이 아닌가?
- `/data/data2` 여유 공간 500GB 이상인가?
- 외부망 4개 전부 `200`/`301` → **경로 A** / 하나라도 `TIMEOUT` → **경로 B**

→ **이 출력을 Claude에게 보여주고 A/B 확정할 것.**

---

## STEP 3 — 작업 폴더 생성 (서버)

```bash
ssh kist-5090 'bash -s' <<'EOF'
export WORK=/data/data2/<USER>/gr00t
mkdir -p $WORK/{dataset,hf_cache,output,logs}
grep -q 'GR00T finetune' ~/.bashrc || cat >> ~/.bashrc <<'BRC'

# ---- GR00T finetune ----
export WORK=/data/data2/<USER>/gr00t
export HF_HOME=$WORK/hf_cache
export HF_HUB_DISABLE_XET=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export UV_CACHE_DIR=$WORK/.uv_cache
BRC
ls -ld $WORK $WORK/*
EOF
```

**[검증]** 5개 디렉토리가 `<USER>` 소유로 생성됨.

---

## STEP 4 — 데이터셋 전송 (DGX Spark에서 실행)

```bash
rsync -avhP --stats \
  /home/edgexpert00/GR00T-WholeBodyControl/outputs/raise_arm_banana_merged \
  kist-5090:/data/data2/<USER>/gr00t/dataset/
```

**[검증]**

```bash
ssh kist-5090 'bash -s' <<'EOF'
D=/data/data2/<USER>/gr00t/dataset/raise_arm_banana_merged
du -sh $D
echo "parquet: $(ls $D/data/chunk-000 | wc -l)  (기대 55)"
echo "mp4    : $(ls $D/videos/chunk-000/observation.images.ego_view | wc -l)  (기대 55)"
python3 -c "import json;d=json.load(open('$D/meta/info.json'));print('splits',d['splits'],'episodes',d['total_episodes'])"
EOF
```

---

## STEP 5 — uv + Isaac-GR00T 설치 (서버)

### 5a. 경로 A (외부망 O)

```bash
ssh kist-5090 'bash -s' <<'EOF'
source ~/.bashrc
which uv || { curl -LsSf https://astral.sh/uv/install.sh | sh; source $HOME/.local/bin/env; }
uv --version
cd $WORK
[ -d Isaac-GR00T ] || git clone https://github.com/NVIDIA/Isaac-GR00T.git
cd Isaac-GR00T && git log -1 --oneline
EOF
```

### 5b. 경로 B (외부망 X) — DGX Spark에서 준비 후 전송

```bash
# DGX Spark
git clone https://github.com/NVIDIA/Isaac-GR00T.git /tmp/Isaac-GR00T
rsync -avhP /tmp/Isaac-GR00T kist-5090:/data/data2/<USER>/gr00t/
# uv 바이너리: 서버 아키텍처(x86_64) 맞는 릴리스를 받아 scp
#   https://github.com/astral-sh/uv/releases → uv-x86_64-unknown-linux-gnu.tar.gz
```

### 5c. 의존성 설치 (공통, 오래 걸림)

```bash
ssh kist-5090
source ~/.bashrc
cd $WORK/Isaac-GR00T
uv sync --all-extras 2>&1 | tee $WORK/logs/uv_sync.log
```

**[검증]** `.venv` 생성, 에러 없이 종료.

---

## STEP 6 — ★RTX 5090 (sm_120) 커널 검증★ 가장 중요

> `torch.cuda.is_available()` 만으로는 못 잡는다. **실제 연산을 돌려야** 한다.
> 4090(sm_89)에서는 안 나타나던 문제다.

```bash
ssh kist-5090
source ~/.bashrc
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
x = torch.randn(4096, 4096, device="cuda:0", dtype=torch.bfloat16)
print("bf16 matmul OK:", (x @ x).float().sum().item())
print("NCCL       :", torch.distributed.is_nccl_available())
n = torch.cuda.device_count()
y = torch.randn(2048, 2048, device="cuda:0")
for i in range(1, n): y = y.to(f"cuda:{i}")
y = y.to("cuda:0"); print(f"{n}-GPU P2P transfer OK")
EOF
```

**[검증]**
- `arch list` 에 `sm_120` 포함
- `bf16 matmul OK: <숫자>` 출력 (여기서 `no kernel image is available` 나오면 아래 실행)
- `NCCL: True`, `4-GPU P2P transfer OK`

**실패 시 복구:**

```bash
cd $WORK/Isaac-GR00T
uv pip install --upgrade --force-reinstall torch torchvision \
  --index-url https://download.pytorch.org/whl/cu128
# 재검증. flash-attn 계열이 깨지면 그것도 재설치 필요
```

---

## STEP 7 — 인증 + 모델 다운로드 (서버)

```bash
ssh kist-5090
source ~/.bashrc
cd $WORK/Isaac-GR00T

uv run hf auth login          # 구버전이면 uv run huggingface-cli login
uv run hf auth whoami
uv run wandb login            # https://wandb.ai/authorize 에서 API key
```

### 경로 A — 서버에서 직접 다운로드

```bash
export HF_HUB_DOWNLOAD_TIMEOUT=60
uv run hf download nvidia/GR00T-N1.7-3B     2>&1 | tail -5
uv run hf download nvidia/Cosmos-Reason2-2B 2>&1 | tail -5
du -sh $HF_HOME
```

> `HF_HUB_DISABLE_XET=1` 이 .bashrc에 있는지 확인. 없으면 KB/s로 떨어져 멈춘 것처럼 보인다.
> Cosmos-Reason2-2B는 gated — 401/403이면 HF 모델 페이지에서 라이선스 동의 먼저.

### 경로 B — 4090 데스크탑 캐시 전송

```bash
# 데스크탑(taeung)에서
rsync -avhP ~/.cache/huggingface/hub/models--nvidia--GR00T-N1.7-3B \
            ~/.cache/huggingface/hub/models--nvidia--Cosmos-Reason2-2B \
  kist-5090:/data/data2/<USER>/gr00t/hf_cache/hub/
```

**[검증]** `du -sh $HF_HOME` 이 수 GB 이상.

---

## STEP 8 — 스모크 테스트 ① 단일 GPU 20 step

```bash
ssh kist-5090
source ~/.bashrc
cd $WORK/Isaac-GR00T
export CUDA_VISIBLE_DEVICES=0

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

**[검증]** 20 step 완주 + `$WORK/output/smoke_1gpu/checkpoint-20` 생성.

---

## STEP 9 — 스모크 테스트 ② 4 GPU 50 step + VRAM 실측

```bash
ssh kist-5090
source ~/.bashrc
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

**돌아가는 동안 다른 터미널에서:**

```bash
ssh kist-5090 'watch -n2 nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv'
```

**[검증] 및 배치 크기 결정**

| 카드당 피크 VRAM | 본 학습 `--global-batch-size` |
|---|---|
| 28GB 초과 / OOM | 8 (그래도 OOM이면 4) |
| 20~28GB | **16** (그대로) |
| 20GB 미만 | 32 로 상향 시도 |

---

## STEP 10 — 본 학습 (반드시 tmux)

```bash
ssh kist-5090
tmux new -s ft
```

tmux 안에서:

```bash
source ~/.bashrc
cd $WORK/Isaac-GR00T
unset CUDA_VISIBLE_DEVICES
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

`Ctrl-b d` detach / `tmux attach -t ft` 복귀.

**첫 체크포인트(step 1000) 직후 반드시 확인 — 용량 폭주 방지:**

```bash
ssh kist-5090 'du -sh /data/data2/<USER>/gr00t/output/raise_arm_banana_n17/checkpoint-1000; df -h /data/data2'
```

체크포인트 1개 × 10개가 `/data/data2` 여유를 넘기면 `--save-total-limit` 을 낮춰 재시작.

---

## STEP 11 — 모니터링

```bash
ssh kist-5090 'tail -f /data/data2/<USER>/gr00t/logs/train_*.log'
ssh kist-5090 'watch -n5 nvidia-smi'
ssh kist-5090 'ls -lh /data/data2/<USER>/gr00t/output/raise_arm_banana_n17/'
```

wandb: `https://wandb.ai/<계정>/g1-sonic-raise-arm-banana`

**중단 판단 기준**
- 초반 수백 step에서 loss가 평평하거나 NaN → 즉시 중단, 데이터/정규화 경로 점검
- loss가 정상 감소 후 plateau → 정상. 이후 실기 평가로 최적 체크포인트 선택

---

## 참고: 자주 나올 에러

| 증상 | 조치 |
|---|---|
| `no kernel image is available` | STEP 6의 cu128 재설치 |
| HF 다운로드가 멈춘 듯 느림 | `export HF_HUB_DISABLE_XET=1` |
| Cosmos-Reason2-2B `401/403` | `hf auth login` + 모델 페이지 라이선스 동의 |
| CUDA OOM | `--global-batch-size` 반감 |
| ssh 끊겨 학습 중단 | tmux 사용 필수 |
| dataloader 느림/정지 | 데이터가 `/data/data2` 로컬에 있는지 확인 (NFS 홈 금지) |
| `/data/data2` full | `--save-total-limit` 축소, smoke 출력 삭제 |
