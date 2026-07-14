#!/usr/bin/env bash
# =============================================================================
# dgx_spark_real_deploy_helper.sh
# DGX Spark (aarch64 / Ubuntu 24.04) — Unitree G1 실로봇 배포 헬퍼
# GR00T-WholeBodyControl / NVIDIA GEAR-SONIC
# =============================================================================
# 사용법: bash dgx_spark_real_deploy_helper.sh <command>
#   preflight            배포 전 전체 사전점검 + MD5 스냅샷 기록
#   boot-verify          재부팅 후 자동복구 검증
#   postflight           배포 후 보존 영역 변경 확인
#   swap-cyclonedds      libddsc 번들을 PR#1817 패치본으로 교체
#   rollback-cyclonedds  libddsc 번들을 오리지널로 복원
#   status               현재 시스템 상태 빠른 확인
# =============================================================================
set -uo pipefail

# ─── ANSI Colors ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ─── Paths ────────────────────────────────────────────────────────────────────
REPO="$HOME/GR00T-WholeBodyControl"
DEPLOY="$REPO/gear_sonic_deploy"
VENV_TELEOP="$REPO/.venv_teleop"
BINARY="$DEPLOY/target/release/g1_deploy_onnx_ref"

# 모델 파일 (deploy.sh 기본값 기준)
MODEL_DECODER="$DEPLOY/policy/release/model_decoder.onnx"
MODEL_ENCODER="$DEPLOY/policy/release/model_encoder.onnx"
OBS_CONFIG="$DEPLOY/policy/release/observation_config.yaml"
PLANNER="$DEPLOY/planner/target_vel/V2/planner_sonic.onnx"
MOTION_DATA="$DEPLOY/reference/example"

# cyclonedds
# libddsc.so.0 는 libddsc.so 의 심링크 → 실제 파일은 libddsc.so
BUNDLED_LIB_DIR="$DEPLOY/thirdparty/unitree_sdk2/thirdparty/lib/aarch64"
BUNDLED_LIBDDSC="$BUNDLED_LIB_DIR/libddsc.so"
PATCHED_LIBDDSC="$HOME/.local/cyclonedds-c/lib/libddsc.so.0.10.2"

# MD5 스냅샷
SNAPSHOT_BASE="/tmp/preflight_snapshot"

# G1 네트워크 인터페이스 (USB 이더넷 어댑터)
G1_IFACE_PREFERRED="enx00e04c682cdf"

# ─── 보존 영역 라벨/경로 병렬 배열 ──────────────────────────────────────────
SNAP_LABELS=(
    "patched_libddsc"
    "bundled_libddsc"
    "binary"
    "cmakelist_dla_patch"
)
SNAP_PATHS=(
    "$PATCHED_LIBDDSC"
    "$BUNDLED_LIBDDSC"
    "$BINARY"
    "$DEPLOY/src/g1/g1_deploy_onnx_ref/CMakeLists.txt"
)

# ─── 카운터 ──────────────────────────────────────────────────────────────────
PASS=0; FAIL=0; WARN=0

pass()   { echo -e "  ${GREEN}✅${NC} $*"; PASS=$((PASS + 1)); }
fail()   { echo -e "  ${RED}❌${NC} $*"; FAIL=$((FAIL + 1)); }
warn()   { echo -e "  ${YELLOW}⚠️ ${NC} $*"; WARN=$((WARN + 1)); }
info()   { echo -e "  ${CYAN}ℹ️ ${NC} $*"; }
header() { echo -e "\n${BOLD}${BLUE}── $* ──${NC}"; }
section(){
    echo -e "\n${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC}  ${BOLD}$*${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
}

summary(){
    echo ""
    echo -e "${BOLD}──────────────────── 요약 ────────────────────${NC}"
    echo -e "  ${GREEN}통과${NC}: $PASS   ${RED}실패${NC}: $FAIL   ${YELLOW}경고${NC}: $WARN"
    if [[ $FAIL -eq 0 && $WARN -eq 0 ]]; then
        echo -e "  ${GREEN}${BOLD}✅ 모든 점검 통과 — 배포 준비 완료${NC}"
    elif [[ $FAIL -eq 0 ]]; then
        echo -e "  ${YELLOW}${BOLD}⚠️  경고 있음 — 확인 후 진행 권장${NC}"
    else
        echo -e "  ${RED}${BOLD}❌ 실패 항목 있음 — 수정 필요${NC}"
    fi
}

# ─── 개별 점검 함수 ───────────────────────────────────────────────────────────

check_cyclonedds_env(){
    header "CycloneDDS 환경변수 (tracing 트리거 위험)"
    local dirty=0
    for var in CYCLONEDDS_URI CYCLONEDDS_HOME CYCLONE_URI DDS_VERBOSITY; do
        local val="${!var:-}"
        if [[ -n "$val" ]]; then
            fail "$var=$val  ← 해제 필요 (do_print_uint32_bitset SIGABRT 위험)"
            dirty=$((dirty + 1))
        fi
    done
    [[ $dirty -eq 0 ]] && pass "CycloneDDS 환경변수 미설정 (안전)"
}

check_cpu_governor(){
    header "CPU 클럭 거버너 (전체 코어)"
    local bad=()
    for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        local core gov
        core=$(echo "$f" | grep -oP 'cpu\d+')
        gov=$(cat "$f" 2>/dev/null || echo "unknown")
        [[ "$gov" != "performance" ]] && bad+=("${core}=${gov}")
    done
    local total
    total=$(ls /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | wc -l)
    if [[ ${#bad[@]} -eq 0 ]]; then
        pass "전체 ${total}코어 governor=performance"
    else
        fail "performance 아닌 코어: ${bad[*]}"
        info "→ sudo systemctl restart nv-cpu-governor.service"
    fi
}

check_gpu_persistence(){
    header "GPU persistence_mode"
    if ! command -v nvidia-smi &>/dev/null; then
        warn "nvidia-smi 없음"; return
    fi
    local mode
    mode=$(nvidia-smi --query-gpu=persistence_mode --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
    if [[ "$mode" == "Enabled" ]]; then
        pass "GPU persistence_mode=Enabled"
    else
        fail "GPU persistence_mode=$mode (Enabled 필요)"
        info "→ sudo nvidia-smi -pm 1"
    fi
}

check_model_files(){
    header "모델 파일 (5개 항목)"
    for f in "$MODEL_DECODER" "$MODEL_ENCODER" "$OBS_CONFIG" "$PLANNER"; do
        if [[ -f "$f" ]]; then
            local sz; sz=$(du -sh "$f" | cut -f1)
            pass "$(basename "$f")  (${sz})"
        else
            fail "없음: $f"
        fi
    done
    if [[ -d "$MOTION_DATA" ]]; then
        local cnt; cnt=$(ls "$MOTION_DATA" | wc -l)
        pass "reference/example/  (${cnt}개 모션 시퀀스)"
    else
        fail "없음: $MOTION_DATA"
    fi
}

check_venv(){
    header ".venv_teleop"
    if [[ -f "$VENV_TELEOP/bin/activate" ]]; then
        pass ".venv_teleop/bin/activate 존재"
        local pyver; pyver=$("$VENV_TELEOP/bin/python" --version 2>&1 || echo "unknown")
        info "Python: $pyver"
    else
        fail ".venv_teleop activate 없음 — install_scripts/install_pico.sh 재실행 필요"
    fi
}

check_patched_cyclonedds(){
    header "패치된 cyclonedds (PR #1817 백포트)"
    if [[ -f "$PATCHED_LIBDDSC" ]]; then
        local sz; sz=$(du -sh "$PATCHED_LIBDDSC" | cut -f1)
        pass "패치본 존재: $PATCHED_LIBDDSC  (${sz})"
    else
        fail "패치본 없음: $PATCHED_LIBDDSC"
        info "→ cyclonedds 재빌드 (Phase 2 절차) 필요"
    fi
}

check_binary(){
    header "C++ 배포 바이너리 + libddsc 링크"
    if [[ ! -f "$BINARY" ]]; then
        fail "바이너리 없음: $BINARY → just build 필요"
        return
    fi
    local sz; sz=$(du -sh "$BINARY" | cut -f1)
    pass "바이너리 존재 (${sz})"

    local ldd_line
    ldd_line=$(ldd "$BINARY" 2>/dev/null | grep "libddsc.so.0" || true)
    if [[ -z "$ldd_line" ]]; then
        warn "ldd에서 libddsc.so.0 미발견"
        return
    fi

    local lib_path
    lib_path=$(echo "$ldd_line" | grep -oP '=> \K\S+' || true)
    if [[ "$lib_path" == "$BUNDLED_LIB_DIR"* ]]; then
        pass "libddsc.so.0 → Unitree 번들 경로"
        info "경로: $lib_path"
        # 스왑 상태 확인
        if [[ -f "$PATCHED_LIBDDSC" ]]; then
            local bmd pmd
            bmd=$(md5sum "$BUNDLED_LIBDDSC" | cut -d' ' -f1)
            pmd=$(md5sum "$PATCHED_LIBDDSC" | cut -d' ' -f1)
            if [[ "$bmd" == "$pmd" ]]; then
                info "libddsc 상태: 패치본 SWAP 적용됨 (MD5 일치)"
            else
                info "libddsc 상태: 오리지널 번들 (미패치)"
            fi
        fi
    else
        warn "libddsc.so.0 → 예상 외 경로: $lib_path"
    fi
}

take_snapshot(){
    local ts; ts=$(date +%Y%m%d_%H%M%S)
    local snap_dir="$SNAPSHOT_BASE/$ts"
    mkdir -p "$snap_dir"
    local snap_file="$snap_dir/md5sums.txt"

    echo "# DGX Spark deploy.sh real preflight snapshot — $ts" > "$snap_file"
    local i
    for i in "${!SNAP_LABELS[@]}"; do
        local label="${SNAP_LABELS[$i]}"
        local path="${SNAP_PATHS[$i]}"
        if [[ -f "$path" ]]; then
            local md5; md5=$(md5sum "$path" | cut -d' ' -f1)
            echo "$md5  $label  $path" >> "$snap_file"
        else
            echo "MISSING  $label  $path" >> "$snap_file"
        fi
    done

    ln -sf "$snap_file" "$SNAPSHOT_BASE/latest.txt" 2>/dev/null || true
    info "스냅샷 저장: $snap_file"
    info "최신 링크:  $SNAPSHOT_BASE/latest.txt"
}

# ─── 서브커맨드 ──────────────────────────────────────────────────────────────

cmd_preflight(){
    section "G1 실로봇 배포 사전점검 (preflight)"
    echo -e "  시각:   $(date)"
    echo -e "  호스트: $(hostname)  [$(uname -m) / $(lsb_release -rs 2>/dev/null || uname -r)]"

    check_cyclonedds_env
    check_cpu_governor
    check_gpu_persistence
    check_model_files
    check_venv
    check_patched_cyclonedds
    check_binary

    header "보존 영역 MD5 스냅샷 기록"
    take_snapshot

    summary
}

cmd_boot_verify(){
    section "재부팅 후 자동복구 검증 (boot-verify)"
    echo -e "  시각: $(date)"

    header "systemd 서비스 상태"
    for svc in nv-cpu-governor.service nvidia-persistenced.service; do
        local enabled active
        enabled=$(systemctl is-enabled "$svc" 2>/dev/null || echo "unknown")
        active=$(systemctl is-active  "$svc" 2>/dev/null || echo "unknown")
        if [[ "$enabled" == "enabled" && "$active" == "active" ]]; then
            pass "$svc: enabled + active"
        elif [[ "$enabled" == "enabled" ]]; then
            warn "$svc: enabled이나 active 아님 (active=$active) — sudo systemctl start $svc"
        else
            fail "$svc: enabled=$enabled, active=$active"
        fi
    done

    check_cpu_governor
    check_gpu_persistence
    summary
}

cmd_postflight(){
    section "배포 후 보존 영역 변경 확인 (postflight)"

    if [[ ! -f "$SNAPSHOT_BASE/latest.txt" ]]; then
        echo -e "${RED}❌ preflight 스냅샷 없음 — 먼저 preflight 실행 필요${NC}"
        exit 1
    fi

    header "스냅샷 비교 (기준: $SNAPSHOT_BASE/latest.txt)"
    local changed=0
    while IFS= read -r line; do
        [[ "$line" =~ ^# ]] && continue
        [[ -z "$line" ]]    && continue
        local old_md5 label path
        read -r old_md5 label path <<< "$line"

        if [[ "$old_md5" == "MISSING" ]]; then
            if [[ -f "$path" ]]; then
                warn "$label: 이전에 없었으나 현재 존재 ($path)"
                changed=$((changed + 1))
            fi
            continue
        fi

        if [[ ! -f "$path" ]]; then
            fail "$label: 파일 사라짐 ($path)"
            changed=$((changed + 1))
            continue
        fi

        local cur_md5; cur_md5=$(md5sum "$path" | cut -d' ' -f1)
        if [[ "$cur_md5" == "$old_md5" ]]; then
            pass "$label: 변경 없음"
        else
            warn "$label: MD5 변경됨"
            info "  이전: $old_md5"
            info "  현재: $cur_md5  ($path)"
            changed=$((changed + 1))
        fi
    done < "$SNAPSHOT_BASE/latest.txt"

    echo ""
    if [[ $changed -eq 0 ]]; then
        echo -e "  ${GREEN}✅ 보존 영역 변경 없음${NC}"
    else
        echo -e "  ${YELLOW}⚠️  ${changed}개 항목 변경 감지 — 의도적 변경인지 확인 필요${NC}"
    fi
}

cmd_swap_cyclonedds(){
    section "cyclonedds SWAP: 번들 → PR#1817 패치본 (swap-cyclonedds)"
    echo ""

    if [[ ! -f "$PATCHED_LIBDDSC" ]]; then
        echo -e "${RED}❌ 패치본 없음: $PATCHED_LIBDDSC${NC}"
        exit 1
    fi
    if [[ ! -f "$BUNDLED_LIBDDSC" ]]; then
        echo -e "${RED}❌ 번들 파일 없음: $BUNDLED_LIBDDSC${NC}"
        exit 1
    fi

    # 이미 스왑됐는지 확인
    local bmd pmd
    bmd=$(md5sum "$BUNDLED_LIBDDSC" | cut -d' ' -f1)
    pmd=$(md5sum "$PATCHED_LIBDDSC" | cut -d' ' -f1)
    if [[ "$bmd" == "$pmd" ]]; then
        echo -e "  ${CYAN}ℹ️ ${NC} 이미 패치본 SWAP이 적용되어 있습니다 (MD5 동일). 중복 실행 불필요."
        exit 0
    fi

    local ts; ts=$(date +%Y%m%d_%H%M%S)
    local backup_path="${BUNDLED_LIB_DIR}/libddsc.so.bak.${ts}"

    echo -e "  교체 대상:"
    echo -e "    번들  (현재): $BUNDLED_LIBDDSC  [MD5: $bmd]"
    echo -e "    패치본 (신규): $PATCHED_LIBDDSC  [MD5: $pmd]"
    echo -e "    백업 경로:     $backup_path"
    echo ""
    read -rp "$(echo -e "${YELLOW}SWAP을 진행하시겠습니까? [y/N]: ${NC}")" confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then echo "취소됨."; exit 0; fi

    cp "$BUNDLED_LIBDDSC" "$backup_path"
    echo -e "  ${GREEN}✅${NC} 백업 완료: $backup_path"

    cp "$PATCHED_LIBDDSC" "$BUNDLED_LIBDDSC"

    # 검증
    local new_md5; new_md5=$(md5sum "$BUNDLED_LIBDDSC" | cut -d' ' -f1)
    if [[ "$new_md5" == "$pmd" ]]; then
        echo -e "  ${GREEN}✅${NC} SWAP 완료 — MD5 검증 통과"
        info "libddsc.so.0 심링크는 그대로 libddsc.so 를 가리킵니다 (변경 불필요)"
        info "rollback-cyclonedds 로 원래 번들 복원 가능"
    else
        echo -e "  ${RED}❌${NC} SWAP 후 MD5 불일치 — 백업에서 수동 복구 필요:"
        echo -e "       cp \"$backup_path\" \"$BUNDLED_LIBDDSC\""
        exit 1
    fi
}

cmd_rollback_cyclonedds(){
    section "cyclonedds 번들 라이브러리 롤백 (rollback-cyclonedds)"
    echo ""

    # 최신 백업 탐색
    local backup
    backup=$(ls -t "${BUNDLED_LIB_DIR}"/libddsc.so.bak.* 2>/dev/null | head -1 || true)
    if [[ -z "$backup" ]]; then
        echo -e "${RED}❌ 백업 파일 없음: ${BUNDLED_LIB_DIR}/libddsc.so.bak.*${NC}"
        echo -e "   swap-cyclonedds 를 실행한 적이 없거나 백업이 삭제됐습니다."
        exit 1
    fi

    local cur_md5 bak_md5
    cur_md5=$(md5sum "$BUNDLED_LIBDDSC" | cut -d' ' -f1)
    bak_md5=$(md5sum "$backup"          | cut -d' ' -f1)

    echo -e "  롤백 정보:"
    echo -e "    현재:  $BUNDLED_LIBDDSC  [MD5: $cur_md5]"
    echo -e "    백업:  $backup  [MD5: $bak_md5]"
    echo ""

    if [[ "$cur_md5" == "$bak_md5" ]]; then
        echo -e "  ${CYAN}ℹ️ ${NC} 현재 파일과 백업이 동일합니다. 롤백 불필요."
        exit 0
    fi

    read -rp "$(echo -e "${YELLOW}롤백을 진행하시겠습니까? [y/N]: ${NC}")" confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then echo "취소됨."; exit 0; fi

    cp "$backup" "$BUNDLED_LIBDDSC"

    local restored_md5; restored_md5=$(md5sum "$BUNDLED_LIBDDSC" | cut -d' ' -f1)
    if [[ "$restored_md5" == "$bak_md5" ]]; then
        echo -e "  ${GREEN}✅ 롤백 완료 — MD5 검증 통과${NC}"
    else
        echo -e "  ${RED}❌ 롤백 후 MD5 불일치 — 수동 확인 필요${NC}"
        exit 1
    fi
}

cmd_status(){
    section "현재 시스템 상태 (status)"
    echo -e "  시각: $(date)"
    echo ""

    header "CPU 거버너"
    local govs; govs=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort | uniq -c | tr '\n' ' ')
    echo -e "    $govs"

    header "GPU persistence_mode"
    if command -v nvidia-smi &>/dev/null; then
        local gpu_info; gpu_info=$(nvidia-smi --query-gpu=name,persistence_mode --format=csv,noheader 2>/dev/null)
        echo -e "    $gpu_info"
    else
        echo -e "    nvidia-smi 없음"
    fi

    header "G1 네트워크 인터페이스"
    if ip link show "$G1_IFACE_PREFERRED" &>/dev/null 2>&1; then
        local state ip_addr
        state=$(ip link show "$G1_IFACE_PREFERRED" 2>/dev/null | grep -oP 'state \K\S+' || echo "unknown")
        ip_addr=$(ip -4 addr show "$G1_IFACE_PREFERRED" 2>/dev/null | grep -oP 'inet \K[\d.]+' || echo "미할당")
        echo -e "    $G1_IFACE_PREFERRED  state=${state}  IP=${ip_addr}"
        if [[ "$state" == "UP" && "$ip_addr" == "192.168.123."* ]]; then
            echo -e "    ${GREEN}✅ G1 연결됨${NC}"
        elif [[ "$state" == "UP" ]]; then
            echo -e "    ${YELLOW}⚠️  인터페이스 UP이나 192.168.123.x 미할당 — G1 전원/케이블 확인${NC}"
        else
            echo -e "    ${YELLOW}⚠️  인터페이스 DOWN — G1 미연결${NC}"
        fi
    else
        # 192.168.123.x 가 다른 인터페이스에 있는지 확인
        local alt
        alt=$(ip -4 addr show 2>/dev/null | grep "192.168.123." | awk '{print $NF}' || true)
        if [[ -n "$alt" ]]; then
            echo -e "    ${GREEN}G1 네트워크 감지 (인터페이스: $alt)${NC}"
        else
            echo -e "    ${YELLOW}⚠️  G1 인터페이스 미연결 ($G1_IFACE_PREFERRED 없음)${NC}"
        fi
    fi

    header "CycloneDDS 환경변수"
    local found_env=0
    for var in CYCLONEDDS_URI CYCLONEDDS_HOME CYCLONE_URI DDS_VERBOSITY; do
        local val="${!var:-}"
        if [[ -n "$val" ]]; then
            echo -e "    ${YELLOW}$var=$val  ← 위험${NC}"
            found_env=$((found_env + 1))
        fi
    done
    [[ $found_env -eq 0 ]] && echo -e "    ${GREEN}미설정 (안전)${NC}"

    header "libddsc SWAP 상태"
    if [[ -f "$BUNDLED_LIBDDSC" && -f "$PATCHED_LIBDDSC" ]]; then
        local bmd pmd
        bmd=$(md5sum "$BUNDLED_LIBDDSC" | cut -d' ' -f1)
        pmd=$(md5sum "$PATCHED_LIBDDSC" | cut -d' ' -f1)
        if [[ "$bmd" == "$pmd" ]]; then
            echo -e "    ${GREEN}패치본 SWAP 적용됨${NC}  (PR #1817 / MD5 일치)"
        else
            echo -e "    오리지널 번들 (미패치)"
        fi
        local bak_cnt; bak_cnt=$(ls "${BUNDLED_LIB_DIR}"/libddsc.so.bak.* 2>/dev/null | wc -l)
        echo -e "    백업 파일: ${bak_cnt}개"
    else
        echo -e "    ${YELLOW}경로 확인 불가${NC}"
    fi

    header "관련 프로세스"
    local procs
    procs=$(pgrep -la "g1_deploy_onnx_ref\|run_sim_loop\|pico_manager" 2>/dev/null || true)
    if [[ -n "$procs" ]]; then
        echo "$procs" | while IFS= read -r line; do echo -e "    $line"; done
    else
        echo -e "    관련 프로세스 없음"
    fi
}

# ─── Usage ───────────────────────────────────────────────────────────────────
usage(){
    echo -e "${BOLD}사용법:${NC}  $(basename "$0") <command>"
    echo ""
    echo -e "  ${CYAN}preflight${NC}            배포 전 전체 사전점검 + MD5 스냅샷 기록"
    echo -e "  ${CYAN}boot-verify${NC}          재부팅 후 자동복구 검증"
    echo -e "  ${CYAN}postflight${NC}           배포 후 보존 영역 변경 확인"
    echo -e "  ${CYAN}swap-cyclonedds${NC}      libddsc 번들을 PR#1817 패치본으로 교체"
    echo -e "  ${CYAN}rollback-cyclonedds${NC}  libddsc 번들을 오리지널로 복원"
    echo -e "  ${CYAN}status${NC}               현재 시스템 상태 빠른 확인"
    echo ""
    echo -e "  설명: DGX Spark (aarch64) 에서 deploy.sh real 실행 전후 점검 도구"
}

# ─── Dispatch ────────────────────────────────────────────────────────────────
case "${1:-}" in
    preflight)            cmd_preflight ;;
    boot-verify)          cmd_boot_verify ;;
    postflight)           cmd_postflight ;;
    swap-cyclonedds)      cmd_swap_cyclonedds ;;
    rollback-cyclonedds)  cmd_rollback_cyclonedds ;;
    status)               cmd_status ;;
    *) usage; exit 1 ;;
esac
