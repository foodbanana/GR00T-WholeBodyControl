# 3. Isaac-GR00T N1.7 파인튜닝 (KIST GPU 서버)

병합·분할한 데이터셋으로 **GR00T N1.7-3B** 를 파인튜닝한다. 학습은 DGX Spark 가 아니라
**KIST 서버 cluster101 (RTX 5090 × 4)** 에서 돌린다.

**이전** ← [2. 데이터셋 전처리 & merge](2_dataset_preprocess_merge.md) · **다음** → [4. 평가](4_evaluation.md)

> 이 문서는 **v2 학습(`rab-v2b-20260806`)의 실제 경로**를 기준으로 한다.
> 처음부터 서버를 세팅하는 절차의 원본은 [server_finetune_runbook.md](server_finetune_runbook.md)
> (481줄) / [server_finetune_checklist.md](server_finetune_checklist.md) (413줄) 이며,
> 그 문서들은 작업 루트를 `/data/data2` 로 잡는다 — 차이는 [§0-2](#0-2-작업-경로--v1-계획과-v2-실제가-다르다) 참조.

---

## 0. 서버

### 0-1. 접속 — 2단 점프

```
DGX Spark
   └─ ssh ─▶ 161.122.21.93:4648        cluster100 (NIS+NFS 마스터, /home 제공)
                └─ ssh ─▶ 192.168.135.101:4648   cluster101 = RTX 5090 × 4  ← 학습
```

DGX Spark 에 SSH config 를 한 번 등록해 두면 `ssh kist-5090` 한 줄로 붙는다:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
grep -q 'Host kist-5090' ~/.ssh/config 2>/dev/null || cat >> ~/.ssh/config <<'EOF'

Host kist-gw
    HostName 161.122.21.93
    Port 4648
    User ltw1203
    ServerAliveInterval 30

Host kist-5090
    HostName 192.168.135.101
    Port 4648
    User ltw1203
    ProxyJump kist-gw
    ServerAliveInterval 30
EOF
chmod 600 ~/.ssh/config

[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id kist-gw       # 비밀번호 1회
ssh-copy-id kist-5090     # 홈이 NFS 공유라 보통 이미 통과됨

ssh kist-5090 'hostname; whoami'   # 무암호로 RTX5090 / ltw1203 이면 성공
```

이 설정은 학습뿐 아니라 **평가**([4번](4_evaluation.md))와 **실기 배포 SSH 터널**([5번](5_deploy.md))에서도
그대로 쓴다.

### 0-2. 작업 경로 — v1 계획과 v2 실제가 다르다

| | 경로 | 비고 |
|---|---|---|
| v1 계획 (`server_finetune_runbook.md`) | `/data/data2/ltw1203/gr00t/{Isaac-GR00T,dataset,hf_cache,output}` | cluster101 **로컬 디스크** 8TB. 런북은 NFS 홈을 피하라고 권고 |
| **v2 실제** ✅ | `~/Isaac-GR00T`, `~/dataset/`, `~/hf_cache/`, `~/groot_output/` | **홈**. 용량 여유가 있어 여기에 두었다 |

**현재 서버에 있는 실물은 홈 경로다.** 이 문서는 그 기준으로 쓴다.

> ⚠️ **다음 학습 때 판단할 것.** `/home` 은 cluster100 에서 NFS 마운트한 공유 홈이다.
> 런북이 "대용량 체크포인트/데이터를 여기 두면 느리고 공용 쿼터를 먹는다,
> dataloader 가 정지/지연되면 데이터가 NFS 홈에 있는지부터 확인하라"고 경고한 경로다.
> v2 에서는 문제없이 돌았지만, 데이터가 커지면 `/data/data2` 로 옮기는 것을 검토한다.
> (`/data/data1` 은 `user:user` 소유라 **쓰기 불가**, `/data/data2` 만 쓰기 가능하다.)

### 0-3. ★ `~/groot_env.sh` — 모든 명령 앞에 붙는다

```bash
source ~/groot_env.sh
```

**핵심은 `export HF_HOME=/home/ltw1203/hf_cache` 다.** VLM 백본
`nvidia/Cosmos-Reason2-2B` 는 **gated 저장소**이고, 토큰과 캐시가 이 경로에만 있다.

| 경로 | 내용 |
|---|---|
| `~/hf_cache/hub/` | `models--nvidia--Cosmos-Reason2-2B`, `models--nvidia--GR00T-N1.7-3B` + `token` ✅ |
| `~/.cache/huggingface/` (HF 기본값) | **비어 있음, 토큰 없음** ❌ |

빠뜨리면 학습에서도, PolicyServer 기동에서도 이렇게 죽는다:

```
RuntimeError: Cannot download the VLM backbone 'nvidia/Cosmos-Reason2-2B',
which is a gated Hugging Face repo.
401 Client Error ... Access to model nvidia/Cosmos-Reason2-2B is restricted.
```

> **`--model-path` 가 로컬 경로여도 백본만은 항상 HF 를 탄다.** 2026-08-07 실제로 확인했다.
> 이 함정은 [5번 문서](5_deploy.md) 의 PolicyServer 기동에서도 똑같이 나온다.

같이 잡아두면 좋은 것들 (`~/.bashrc` 또는 `groot_env.sh`):

```bash
export HF_HOME=/home/ltw1203/hf_cache
export HF_HUB_DISABLE_XET=1                        # ← 없으면 다운로드가 KB/s 로 떨어진다
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

---

## 1. 환경 구축 — **처음 세팅할 때만**

서버에 이미 `~/Isaac-GR00T` 와 `~/hf_cache` 가 있으면 [§2](#2-데이터셋-전송) 로 건너뛴다.

### 1-1. 서버 상태 점검

```bash
ssh kist-5090 'bash -s' <<'EOF'
echo "===== HOST ====="; hostname; whoami; id
echo "===== GPU ====="; nvidia-smi
echo "===== GPU 점유자 ====="; nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv
echo "===== DISK ====="; df -h /home /data/data1 /data/data2
echo "===== CPU/RAM ====="; nproc; free -h
echo "===== 외부망 ====="
for u in https://huggingface.co https://github.com https://api.wandb.ai https://astral.sh; do
  code=$(timeout 10 curl -sI -o /dev/null -w '%{http_code}' "$u" 2>/dev/null || echo TIMEOUT)
  echo "  $u -> $code"
done
echo "===== 기존 도구 ====="; which uv git tmux python3 nvcc 2>/dev/null; python3 -V
EOF
```

**분기 판단**

- 외부망 4개 모두 `200`/`301` → **경로 A** (서버에서 직접 다운로드)
- 하나라도 `TIMEOUT`/`000` → **경로 B** (DGX Spark 에서 통째로 전송)
- 남의 프로세스가 GPU 를 물고 있으면 사용할 GPU 수를 조정한다 (**공용 서버다**)

### 1-2. uv + Isaac-GR00T

**경로 A**

```bash
ssh kist-5090
which uv || { curl -LsSf https://astral.sh/uv/install.sh | sh; source $HOME/.local/bin/env; }
uv --version

cd ~
[ -d Isaac-GR00T ] || git clone https://github.com/NVIDIA/Isaac-GR00T.git
cd Isaac-GR00T && git log -1 --oneline

export UV_CACHE_DIR=~/.uv_cache
uv sync --all-extras 2>&1 | tee ~/logs/uv_sync.log
```

**경로 B** — DGX Spark 에서 준비해 전송

```bash
# DGX Spark
git clone https://github.com/NVIDIA/Isaac-GR00T.git /tmp/Isaac-GR00T
rsync -avhP /tmp/Isaac-GR00T kist-5090:~/
# uv 는 서버 아키텍처(x86_64)에 맞는 릴리스를 받아 scp
#   https://github.com/astral-sh/uv/releases → uv-x86_64-unknown-linux-gnu.tar.gz
```

### 1-3. ★★ RTX 5090 (Blackwell, sm_120) 커널 검증 — 가장 중요

RTX 5090 은 compute capability **12.0** 이다. PyTorch 가 cu124 이하 빌드면
`torch.cuda.is_available()` 은 **True 를 반환하면서** 실제 커널 실행에서
`no kernel image is available for execution on the device` 로 죽는다.

> **`is_available()` 체크로는 못 잡는다. 실제 연산을 돌려야 한다.**
> 4090(sm_89)에서는 나타나지 않던 문제라, 데스크탑에서 통과했어도 서버에서는 다시 확인한다.

```bash
ssh kist-5090
source ~/groot_env.sh
cd ~/Isaac-GR00T
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
# 실제 커널 실행 — 여기서 죽으면 torch 재설치 필요
x = torch.randn(4096, 4096, device="cuda:0", dtype=torch.bfloat16)
print("bf16 matmul OK:", (x @ x).float().sum().item())
print("NCCL       :", torch.distributed.is_nccl_available())
# 멀티 GPU P2P
n = torch.cuda.device_count()
y = torch.randn(2048, 2048, device="cuda:0")
for i in range(1, n): y = y.to(f"cuda:{i}")
y = y.to("cuda:0"); print(f"{n}-GPU P2P transfer OK")
EOF
```

**합격 기준**: `arch list` 에 `sm_120` 포함 / `bf16 matmul OK: <숫자>` 출력 /
`NCCL: True` / `4-GPU P2P transfer OK`

**실패 시 복구**

```bash
cd ~/Isaac-GR00T
uv pip install --upgrade --force-reinstall torch torchvision \
  --index-url https://download.pytorch.org/whl/cu128
# 재검증. flash-attn 계열이 깨지면 그것도 재설치 필요
```

### 1-4. HuggingFace 인증 + 모델 가중치 + wandb

```bash
cd ~/Isaac-GR00T
uv run hf auth login          # 구버전이면 uv run huggingface-cli login
uv run hf auth whoami
uv run wandb login            # https://wandb.ai/authorize 에서 API key
```

**경로 A** — 서버에서 직접 다운로드

```bash
source ~/groot_env.sh
export HF_HUB_DOWNLOAD_TIMEOUT=60
uv run hf download nvidia/GR00T-N1.7-3B     2>&1 | tail -5
uv run hf download nvidia/Cosmos-Reason2-2B 2>&1 | tail -5
du -sh $HF_HOME        # 수 GB 이상이면 OK
```

**경로 B** — 이미 받아둔 캐시를 전송

```bash
# 캐시가 있는 머신에서
rsync -avhP ~/.cache/huggingface/hub/models--nvidia--GR00T-N1.7-3B \
            ~/.cache/huggingface/hub/models--nvidia--Cosmos-Reason2-2B \
  kist-5090:~/hf_cache/hub/
```

> `Cosmos-Reason2-2B` 는 gated 다. `401/403` 이 나오면 **HF 모델 페이지에서 라이선스 동의**를 먼저 해야 한다.
> 다운로드가 KB/s 로 떨어져 멈춘 것처럼 보이면 `HF_HUB_DISABLE_XET=1` 이 빠진 것이다.

---

## 2. 데이터셋 전송

DGX Spark 에서 실행한다 ([2번 문서](2_dataset_preprocess_merge.md) 산출물).

```bash
rsync -avhP --stats \
  ~/GR00T-WholeBodyControl/outputs/raise_arm_banana_merged \
  kist-5090:~/dataset/
```

서버에서 검증:

```bash
ssh kist-5090 'bash -s' <<'EOF'
D=~/dataset/raise_arm_banana_merged
du -sh $D
echo "parquet: $(ls $D/data/chunk-000 | wc -l)"
echo "mp4    : $(ls $D/videos/chunk-000/observation.images.ego_view | wc -l)"
python3 -c "import json;d=json.load(open('$D/meta/info.json'));print('splits',d['splits'],'episodes',d['total_episodes'],'fps',d['fps'])"
EOF
```

`splits` 가 `0:<전체 에피소드 수>` 인지 반드시 확인한다 — 여기가 틀리면 **조용히 일부만 학습된다.**

> **평가용 val 셋도 같이 올린다.** val 셋은 `meta/stats.json` 이 있어야 로더가 죽지 않는다.
> 없으면 서버에서 `gr00t/data/stats.py` 로 먼저 만든다 ([4번 문서](4_evaluation.md) 참조).

---

## 3. 스모크 테스트 — 본 학습 전 필수

데스크탑에서 통과했더라도 **서버는 GPU 아키텍처·드라이버·경로가 전부 다르다.**

### 3-1. 단일 GPU 20 step

```bash
ssh kist-5090
source ~/groot_env.sh
cd ~/Isaac-GR00T
export CUDA_VISIBLE_DEVICES=0

uv run python gr00t/experiment/launch_finetune.py \
    --base-model-path nvidia/GR00T-N1.7-3B \
    --dataset-path ~/dataset/raise_arm_banana_merged \
    --embodiment-tag UNITREE_G1_SONIC \
    --modality-config-path gr00t/configs/data/embodiment_configs.py \
    --output-dir ~/groot_output/smoke_1gpu \
    --num-gpus 1 \
    --global-batch-size 1 \
    --max-steps 20 \
    --save-steps 20 \
    --save-total-limit 1 \
    --save-only-model \
    --dataloader-num-workers 0 \
    --no-use-wandb 2>&1 | tee ~/logs/smoke_1gpu.log
```

**[검증]** 20 step 완주 + `~/groot_output/smoke_1gpu/checkpoint-20` 생성.

### 3-2. 4 GPU 50 step + VRAM 실측 (NCCL·샤딩 검증)

```bash
unset CUDA_VISIBLE_DEVICES

uv run python gr00t/experiment/launch_finetune.py \
    --base-model-path nvidia/GR00T-N1.7-3B \
    --dataset-path ~/dataset/raise_arm_banana_merged \
    --embodiment-tag UNITREE_G1_SONIC \
    --modality-config-path gr00t/configs/data/embodiment_configs.py \
    --output-dir ~/groot_output/smoke_4gpu \
    --num-gpus 4 \
    --global-batch-size 16 \
    --max-steps 50 \
    --save-steps 50 \
    --save-total-limit 1 \
    --save-only-model \
    --dataloader-num-workers 4 \
    --no-use-wandb 2>&1 | tee ~/logs/smoke_4gpu.log
```

**돌아가는 동안 다른 터미널에서:**

```bash
ssh kist-5090 'watch -n2 nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv'
```

| 카드당 피크 VRAM | 본 학습 `--global-batch-size` |
|---|---|
| 28GB 초과 / OOM | 8 (그래도 OOM 이면 4) |
| 20~28GB | 16 |
| 20GB 미만 | **32 로 상향** ← v2 가 이 경로였다 |

5090 은 카드당 32GB 다 (A100/H100 80GB 가 아니다).

---

## 4. 본 학습

**반드시 tmux 안에서 실행한다** — SSH 가 끊겨도 학습이 살아 있어야 한다.

```bash
ssh kist-5090
tmux new -s ft
```

tmux 안에서:

```bash
source ~/groot_env.sh
cd ~/Isaac-GR00T
unset CUDA_VISIBLE_DEVICES
export WANDB_PROJECT=g1-sonic-raise-arm-banana
export WANDB_NAME=rab-v2b-20260806

uv run python gr00t/experiment/launch_finetune.py \
    --base-model-path nvidia/GR00T-N1.7-3B \
    --dataset-path ~/dataset/raise_arm_banana_v2_train \
    --embodiment-tag UNITREE_G1_SONIC \
    --modality-config-path gr00t/configs/data/embodiment_configs.py \
    --output-dir ~/groot_output/rab-v2b-20260806 \
    --num-gpus 4 \
    --global-batch-size 32 \
    --max-steps 24000 \
    --save-steps 500 \
    --color-jitter-params brightness 0.3 contrast 0.4 saturation 0.5 hue 0.08 \
    --dataloader-num-workers 4 \
    --use-wandb 2>&1 | tee ~/train_rab_v2b.log
```

`Ctrl-b d` 로 detach, `tmux attach -t ft` 로 복귀.

> ⚠️ **위 명령은 v2 산출물에서 역산한 재구성이다.** 확정된 값은 출력 경로
> `~/groot_output/rab-v2b-20260806`, 로그 `~/train_rab_v2b.log`, `--max-steps 24000`,
> `--save-steps 500`(체크포인트 48개), `--global-batch-size 32`, GPU 4장이다.
> `--dataset-path` 의 정확한 폴더명, `--save-total-limit`, val/loss 를 켠 인자는
> **서버에서 확인해 채워 넣을 것**:
> `head -50 ~/train_rab_v2b.log` 또는 `grep -h "launch_finetune.py" ~/.bash_history | tail -5`

### 첫 체크포인트 직후 반드시 확인 — 용량 폭주 방지

```bash
ssh kist-5090 'du -sh ~/groot_output/rab-v2b-20260806/checkpoint-500; df -h /home'
```

`--save-only-model` 을 **넣지 않으면** 체크포인트에 옵티마이저 상태가 포함돼 하나에 수십 GB 가 된다
(중단 후 재개하려면 필요하다). 48개를 남기는 설정이므로 첫 저장 직후 실제 크기를 보고
`--save-total-limit` 을 정한다.

### 플래그 선택 근거

| 플래그 | NVIDIA 문서 | **v2** | 이유 |
|---|---|---|---|
| `--max-steps` | 20000 | **24000** | v1 은 10000(55ep 기준 과적합 우려). v2 는 데이터가 늘어 더 길게 돌렸다 |
| `--global-batch-size` | 32 | **32** | 3-2 VRAM 실측 결과 5090 32GB 에서 여유. v1 은 16 이었다 |
| `--save-steps` | 5000 | **500** | 데이터가 작아 수렴이 빠르다. 촘촘히 남겨 비교 — **체크포인트 선택이 곧 성능**이기 때문 |
| `--embodiment-tag` | — | `UNITREE_G1_SONIC` | 고정 |
| `--color-jitter-params` | — | brightness .3 / contrast .4 / saturation .5 / hue .08 | 조명 변화 대응 |
| `--dataloader-num-workers` | — | 4 | |

> **`--save-steps` 를 촘촘히 두는 것이 이 과제의 핵심 선택이다.** v2 에서 확인된 바로는
> **val/loss 로 체크포인트를 고르면 안 된다** — 관절공간 성능과 상관이 **−0.605 로 방향이 반대**다.
> 학습이 끝난 뒤 [4번 문서](4_evaluation.md)의 관절공간 평가로 골라야 하므로, 후보를 많이 남겨야 한다.

---

## 5. 모니터링 & 완료 확인

```bash
# 진행 로그
ssh kist-5090 'tail -f ~/train_rab_v2b.log'

# GPU
ssh kist-5090 'watch -n5 nvidia-smi'

# 체크포인트
ssh kist-5090 'ls ~/groot_output/rab-v2b-20260806/ | grep checkpoint | sort -t- -k2 -n | tr "\n" " "'
```

wandb: `https://wandb.ai/<계정>/g1-sonic-raise-arm-banana`

**중단 판단**
- 초반 수백 step 에서 loss 가 평평하거나 NaN → **즉시 중단**, 데이터/정규화 경로 점검
- 정상 감소 후 plateau → 정상

### val/loss 는 로그에서 뽑는다

```
step 24000  val/loss = 0.222094
```

> **로그 키는 `val/loss` 다.** `eval_loss` 로 grep 하면 0건이라 "없다"고 오판하기 쉽다.
> v2 는 200 step 간격으로 120개가 기록됐다
> ([eval_results/decoded_openloop_v2_h10/valloss.tsv](../eval_results/decoded_openloop_v2_h10/valloss.tsv)).

### 학습 완료 체크

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
**val/loss 최저 지점을 기록해 둔다** — 이후 해석의 기준선이 된다(정답이 아니라 기준선이다).

이후는 [4. 평가](4_evaluation.md) 로.

---

## 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `no kernel image is available` | torch 가 sm_120 미지원 → [§1-3](#1-3--rtx-5090-blackwell-sm_120-커널-검증--가장-중요) 의 cu128 재설치 |
| `401/403 ... Cosmos-Reason2-2B` | `source ~/groot_env.sh` 누락(= `HF_HOME` 미설정), 또는 라이선스 미동의 |
| HF 다운로드가 KB/s 로 멈춘 듯 | Xet 백엔드 → `export HF_HUB_DISABLE_XET=1` |
| CUDA OOM | `--global-batch-size` 반감 (32 → 16 → 8). 그래도 안 되면 `--no-tune-diffusion-model` (단, 성능 저하) |
| 학습 중 ssh 끊겨 중단 | tmux 미사용. `tmux new -s ft` 안에서 실행할 것 |
| dataloader 정지/느림 | 데이터 위치 확인. **NFS 홈에서 느리면 `/data/data2` 로 옮긴다** ([§0-2](#0-2-작업-경로--v1-계획과-v2-실제가-다르다)) |
| 디스크 full | `--save-total-limit` 축소, smoke 출력(`~/groot_output/smoke_*`) 삭제 |
| 55개 병합했는데 일부만 학습됨 | 데이터셋 `info.json` 의 `splits` 문제 → [2번 문서 STEP 4](2_dataset_preprocess_merge.md#step-4--정제본-재검증) |
| GPU 를 남이 쓰고 있음 | 공용 서버다. `nvidia-smi --query-compute-apps` 로 확인하고 `--num-gpus` 조정 |
