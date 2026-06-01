#!/usr/bin/env bash
# =============================================================================
# seed.sh — Phoenix DevOps OS / Helix Lightning Kernel
# Seed the local clonepool from D1 glossary on a fresh machine.
# Queries the Cloudflare worker glossary, checks local clonepool,
# intakes anything missing. Frank boots with a full closet.
# =============================================================================
# Usage:
#   ./seed.sh              # seed from D1 glossary
#   ./seed.sh --dry-run    # show what would be intaked, don't do it
# =============================================================================

set -euo pipefail

WORKER_URL="${PHOENIX_WORKER_URL:-https://packages-worker.phoenix-jwl.workers.dev}"
PHOENIX_AUTH="${PHOENIX_AUTH:-}"
CLONEPOOL_DIR="${CLONEPOOL_DIR:-${HOME}/Phoenix/clonepool}"
INTAKE="${HOME}/Helix_Lightning_Kernel/intake.sh"
DRY_RUN=false
SEEDED=0
SKIPPED=0
FAILED=0
MISSING=0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [seed:${1}] ${2}"; }

# ── Args ─────────────────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=true ;;
  esac
done

# ── Preflight ─────────────────────────────────────────────────────────────────
[[ -z "${PHOENIX_AUTH}" ]] && { log "ERROR" "PHOENIX_AUTH not set"; exit 1; }
[[ ! -x "${INTAKE}" ]]     && { log "ERROR" "intake.sh not found or not executable: ${INTAKE}"; exit 1; }
command -v curl   &>/dev/null || { log "ERROR" "curl not found"; exit 1; }
command -v python3 &>/dev/null || { log "ERROR" "python3 not found"; exit 1; }

log "INFO" "Phoenix Seed — pulling glossary from D1"
log "INFO" "Worker: ${WORKER_URL}"
log "INFO" "Clonepool: ${CLONEPOOL_DIR}"
[[ "${DRY_RUN}" == true ]] && log "INFO" "DRY RUN — nothing will be intaked"

# ── Pull glossary from D1 ─────────────────────────────────────────────────────
GLOSSARY=$(curl -sf \
  -H "Authorization: Bearer ${PHOENIX_AUTH}" \
  "${WORKER_URL}/glossary") || { log "ERROR" "Failed to reach worker"; exit 1; }

TOTAL=$(echo "${GLOSSARY}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(len(data.get('glossary', [])))
")

log "INFO" "Glossary entries: ${TOTAL}"

# ── Check each entry against local clonepool ──────────────────────────────────
echo "${GLOSSARY}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for entry in data.get('glossary', []):
    print(entry['name'] + '|' + entry['version'] + '|' + entry['hex'])
" | while IFS='|' read -r name version hex; do

  # Check if this suit exists in local clonepool by name
  EXISTING=$(find "${CLONEPOOL_DIR}" -name "*_${name}" 2>/dev/null | head -1)

  if [[ -n "${EXISTING}" ]]; then
    log "INFO" "HAVE   ${name} (${version})"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Not in local clonepool
  log "WARN" "MISSING ${name} (${version}) hex=${hex}"
  MISSING=$((MISSING + 1))

  [[ "${DRY_RUN}" == true ]] && continue

  # Look for the file in the Lightning repo
  REPO_FILE=$(find "${HOME}/Helix_Lightning_Kernel" -name "${name}" 2>/dev/null | head -1)

  if [[ -n "${REPO_FILE}" ]]; then
    log "INFO" "SEEDING ${name} from repo"
    if "${INTAKE}" "${REPO_FILE}" > /dev/null 2>&1; then
      log "INFO" "SEEDED  ${name}"
      SEEDED=$((SEEDED + 1))
    else
      log "ERROR" "FAILED  ${name}"
      FAILED=$((FAILED + 1))
    fi
  else
    log "WARN" "ABSENT  ${name} — not in repo, manual intake required"
    FAILED=$((FAILED + 1))
  fi

done

# ── Summary ───────────────────────────────────────────────────────────────────
log "INFO" "────────────────────────────────"
log "INFO" "Seed complete"
log "INFO" "  Total in glossary : ${TOTAL}"
log "INFO" "  Already present   : ${SKIPPED}"
log "INFO" "  Seeded            : ${SEEDED}"
log "INFO" "  Not in repo       : ${FAILED}"
log "INFO" "────────────────────────────────"
[[ "${FAILED}" -gt 0 ]] && log "WARN" "Some suits need manual intake — check above"
exit 0
