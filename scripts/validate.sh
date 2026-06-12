#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

docker compose --profile '*' config --quiet
python3 ./scripts/validate-access-config.py
bash -n ./scripts/*.sh
python3 -m py_compile ./scripts/*.py ./tests/*.py
python3 -m unittest discover -s tests -p 'test_*.py'

printf '[validate] all local checks passed\n'
