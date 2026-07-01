# Phase 5: Destroy & Cleanup

Destroy tlumi-managed resources, then clean up all Azure resources created
during testing. This phase ALWAYS runs, even if earlier phases failed.

Reference scenarios: 11, 15

**Prerequisites**: `WORKDIR`, `PREFIX`, `SUBSCRIPTION_ID`, `LOCATION` available.
Phase 2 resources may or may not still exist depending on earlier results.

---

## Setup

```bash
mkdir -p "${OUTPUT_DIR}/phase-5-cleanup"

# Define log header function (MUST be called before every tee)
_log_header() {
  local logfile="$1"
  local cmd="$2"
  printf "# CWD: %s\n# CMD: %s\n# TS: %s\n---\n" "$(pwd)" "${cmd}" "$(date -Iseconds)" > "${logfile}"
}
```

If `WORKDIR` exists and contains a valid tlumi project, `cd` into it.
Otherwise, skip the tlumi destroy tests and go straight to Azure cleanup.

---

## Test X01: tlumi destroy with targeted resource (Scenario 11)

First, switch back to local state if currently on remote (to avoid backend
dependency during destroy). If state is remote, pull and switch to local:

```bash
cd "${WORKDIR}"

# If backend.url is set in tlumi.yaml, switch to local state first
if grep -q "backend:" tlumi.yaml 2>/dev/null; then
  tlumi state pull > /tmp/tlumi-test-state-backup.json 2>/dev/null
  tlumi clean --include-state --auto-approve 2>&1 >/dev/null

  # Remove backend section from tlumi.yaml (no PyYAML dependency)
  python3 -c "
lines = open('tlumi.yaml').readlines()
out, skip = [], False
for line in lines:
    if line.startswith('backend:'):
        skip = True
        continue
    if skip and (line.startswith(' ') or line.startswith('\t')):
        continue
    skip = False
    out.append(line)
open('tlumi.yaml', 'w').writelines(out)
"

  echo "" | tlumi init 2>&1 >/dev/null
  tlumi state push /tmp/tlumi-test-state-backup.json --auto-approve 2>&1 >/dev/null
fi

# Test targeted destroy (just the storage account, keep the RG)
_log_header "${OUTPUT_DIR}/phase-5-cleanup/X01-destroy-targeted.log" "tlumi destroy -t storage --var prefix=${PREFIX} --auto-approve"
tlumi destroy -t storage --var prefix=${PREFIX} --auto-approve 2>&1 | tee -a "${OUTPUT_DIR}/phase-5-cleanup/X01-destroy-targeted.log"
```

**Assertions**:
- Exit code is 0
- Output shows delete operation for StorageAccount
- `az storage account show --name "st${PREFIX}dev" --resource-group "rg-${PREFIX}-dev"` fails (resource gone)

## Test X02: tlumi destroy remaining resources (Scenario 11)

```bash
tlumi destroy --var prefix=${PREFIX} --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-5-cleanup/X02-destroy-all.log"
```

**Assertions**:
- Exit code is 0
- Output shows delete operations for remaining resources

## Test X03: Verify resources destroyed in Azure (Scenario 11)

```bash
az group show --name "rg-${PREFIX}-dev" -o json 2>&1 | tee "${OUTPUT_DIR}/phase-5-cleanup/X03-verify-destroyed.log"
X03_EXIT=$?
echo "exit_code=${X03_EXIT}" >> "${OUTPUT_DIR}/phase-5-cleanup/X03-verify-destroyed.log"
```

**Assertions**:
- `az group show` fails (resource group is gone or being deleted)

## Test X04: tlumi clean after destroy (Scenario 15)

```bash
tlumi clean --include-state --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-5-cleanup/X04-clean.log"
```

**Assertions**:
- Exit code is 0
- `.tlumi/` directory is gone

---

## Azure Resource Cleanup

**This section ALWAYS runs, regardless of test results.** It ensures no test
resources are left in the Azure subscription.

Delete resource groups created during testing. Use `--no-wait` for parallel
deletion, then wait for completion.

```bash
echo "--- Azure Resource Cleanup ---" | tee "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log"

# Delete the test infrastructure resource group
az group delete --name "rg-${PREFIX}-dev" --yes --no-wait 2>&1 | tee -a "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log"

# Delete the imported resource group (if it still exists from Phase 2)
az group delete --name "rg-${PREFIX}-imported" --yes --no-wait 2>&1 | tee -a "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log"

# Delete the state storage resource group (if Phase 3 ran)
az group delete --name "rg-${PREFIX}-state" --yes --no-wait 2>&1 | tee -a "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log"

echo "Resource group deletions initiated (async). Waiting for completion..."

# Wait for deletions to complete (check periodically)
for RG_NAME in "rg-${PREFIX}-dev" "rg-${PREFIX}-imported" "rg-${PREFIX}-state"; do
  while az group show --name "${RG_NAME}" -o none 2>/dev/null; do
    echo "  Waiting for ${RG_NAME} deletion..."
    sleep 15
  done
  echo "  ${RG_NAME}: deleted (or did not exist)"
done | tee -a "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log"

echo "All Azure resources cleaned up." | tee -a "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log"
```

### Role Assignment Cleanup

Role assignment cleanup (best-effort; role is typically auto-deleted when the
storage account's resource group is deleted):

```bash
SA_RESOURCE_ID=$(cat "${OUTPUT_DIR}/phase-3-remote/SA_RESOURCE_ID" 2>/dev/null)
USER_OID=$(cat "${OUTPUT_DIR}/phase-3-remote/USER_OID" 2>/dev/null)
if [ -n "${SA_RESOURCE_ID}" ] && [ -n "${USER_OID}" ]; then
  az role assignment delete \
    --role "Storage Blob Data Contributor" \
    --assignee "${USER_OID}" \
    --scope "${SA_RESOURCE_ID}" \
    2>&1 | tee -a "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log" || true
  echo "Role assignment cleanup attempted (may already be removed with RG)." | tee -a "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log"
fi
```

### Temp Directory Cleanup

```bash
if [ -n "${WORKDIR}" ] && [ -d "${WORKDIR}" ]; then
  rm -rf "${WORKDIR}"
  echo "Temp directory cleaned: ${WORKDIR}" | tee -a "${OUTPUT_DIR}/phase-5-cleanup/cleanup.log"
fi
```

---

## Phase 5 Summary

Tests: 4 (X01-X04)
Cleanup: Always runs (3 resource groups + role assignment + temp dir)

---

## Full Test Suite Summary

| Phase | Tests | IDs |
|-------|-------|-----|
| 1: Local-only | 29 | L01-L29 |
| 2: Deploy | 24 | D01-D24 |
| 3: Remote state | 7 | R01-R07 |
| 4: Auth patterns | 3 | R08-R10 |
| 5: Destroy & cleanup | 4 | X01-X04 |
| **Total** | **67** | |
