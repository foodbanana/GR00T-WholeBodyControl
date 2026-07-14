#!/bin/bash
# Wrapper to run the MuJoCo sim loop (Terminal 1) with a CycloneDDS config
# that restricts the Python cyclonedds backend to the loopback interface.
#
# Scope: .venv_sim only. Does not touch gear_sonic_deploy/ or
# thirdparty/unitree_sdk2/, and has no effect on deploy.sh (C++ binary
# uses a separate prebuilt libddsc.so).
set -e

REPO_ROOT="$HOME/GR00T-WholeBodyControl"

export CYCLONEDDS_URI="file://$HOME/.config/cyclonedds/sim_lo_only.xml"

# Locally-built cyclonedds 0.10.2 with the do_print_uint32_bitset
# buffer-overflow backport (PR #1817). Required so both the _clayer.so
# direct link (LD_LIBRARY_PATH) and the cyclonedds.internal ctypes
# loader (CYCLONEDDS_HOME) resolve to this patched libddsc, not the
# system libddsc.so.0debian.
export CYCLONEDDS_HOME="$HOME/.local/cyclonedds-c"
export LD_LIBRARY_PATH="$HOME/.local/cyclonedds-c/lib:$LD_LIBRARY_PATH"

source "$REPO_ROOT/.venv_sim/bin/activate"
cd "$REPO_ROOT"

python -X faulthandler gear_sonic/scripts/run_sim_loop.py "$@"
