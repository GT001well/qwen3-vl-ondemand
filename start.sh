#!/usr/bin/env bash
# Start the on-demand VL model relay.
# Terminal closes → relay dies → backend auto-kills via PDEATHSIG.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 vl-relay.py
