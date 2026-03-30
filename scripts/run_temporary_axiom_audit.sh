#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUDIT_FILE="${1:-${TEMPORARY_AXIOM_AUDIT_FILE:-TemporaryAxiomAudit.lean}}"

cd "${ROOT_DIR}"
if [[ ! -f "${AUDIT_FILE}" ]]; then
  echo "Temporary axiom audit file not found: ${AUDIT_FILE}" >&2
  echo "Create it from templates/TemporaryAxiomAudit.lean and import your host project modules." >&2
  exit 1
fi

echo "Running temporary axiom closure audit via ${AUDIT_FILE}"
lake env lean "${AUDIT_FILE}"
