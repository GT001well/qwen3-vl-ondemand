#!/usr/bin/env bash
# Stop the on-demand VL model relay.
set -euo pipefail
pkill -f "vl-relay\\.py" 2>/dev/null && echo "stopped" || echo "not running"
