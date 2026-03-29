#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "${ROOT_DIR}"
echo "Running temporary theorem closure audit via TemporaryTheoremAudit.lean"
lake env lean TemporaryTheoremAudit.lean
