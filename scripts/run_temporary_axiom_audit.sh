#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "${ROOT_DIR}"
echo "Running temporary axiom closure audit via TemporaryAxiomAudit.lean"
lake env lean TemporaryAxiomAudit.lean
