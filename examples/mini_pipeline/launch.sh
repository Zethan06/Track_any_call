#!/usr/bin/env bash
# Use an absolute script path so this launcher also works from another directory.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON:-python}" "${script_dir}/train.py" "$@"
