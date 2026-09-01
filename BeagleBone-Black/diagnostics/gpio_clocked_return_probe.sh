#!/bin/bash
# Root-only bounded GPIO fallback probe; see sibling Python file for safety.
set -euo pipefail
echo "$BB_PW" | sudo -S -p '' python3 "$HOME/pru/diagnostics/gpio_clocked_return_probe.py"
