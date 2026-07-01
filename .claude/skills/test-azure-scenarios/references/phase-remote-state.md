# Phase 3-4: Remote State & Auth Patterns

Migrate state from local to Azure Blob Storage, test remote operations, then
test SAS token and Azure AD authentication for the storage backend.

Reference scenarios: 2, 12, 17 (SAS + Azure AD)

**Prerequisites**: Phase 2 completed (resources deployed with local state),
`WORKDIR` still intact, Azure CLI logged in.

---

## Phase 3: Remote State Migration (Scenario 2)

### Setup: Create State Storage Infrastructure

Create a dedicated resource group and storage account for state storage.

**NOTE**: All tests MUST use `_log_header` before `tee -a` on every log file.
Define the helper at the start of each phase execution context:

```bash
mkdir -p "${OUTPUT_DIR}/phase-3-remote"

# Define log header function (MUST be called before every tee)
_log_header() {
  local logfile="$1"
  local cmd="$2"
  printf "# CWD: %s\n# CMD: %s\n# TS: %s\n---\n" "$(pwd)" "${cmd}" "$(date -Iseconds)" > "${logfile}"
}

# Create state resource group
az group create \
  --name "rg-${PREFIX}-state" \
  --location "${LOCATION}" \
  --tags managed-by=tlumi-test purpose=state-storage \
  -o json 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R00-create-state-rg.log"

# Create storage account (name must be globally unique, lowercase, no hyphens)
STATE_ACCOUNT="st${PREFIX}state"
az storage account create \
  --name "${STATE_ACCOUNT}" \
  --resource-group "rg-${PREFIX}-state" \
  --location "${LOCATION}" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --allow-blob-public-access false \
  -o json 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R00-create-state-sa.log"

# Create blob container for state
az storage container create \
  --name tfstate \
  --account-name "${STATE_ACCOUNT}" \
  --auth-mode login \
  -o json 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R00-create-container.log"
```

If container creation with `--auth-mode login` fails (missing role assignment),
fall back to key-based auth:

```bash
STATE_KEY=$(az storage account keys list --account-name "${STATE_ACCOUNT}" --resource-group "rg-${PREFIX}-state" --query "[0].value" -o tsv)
az storage container create \
  --name tfstate \
  --account-name "${STATE_ACCOUNT}" \
  --account-key "${STATE_KEY}" \
  -o json
```

Assign Storage Blob Data Contributor role. This is required for Pulumi's azblob
backend (data plane access), even though container creation via management plane
may succeed without it:

```bash
USER_OID=$(az ad signed-in-user show --query "id" -o tsv)
SA_RESOURCE_ID=$(az storage account show \
  --name "${STATE_ACCOUNT}" \
  --resource-group "rg-${PREFIX}-state" \
  --query "id" -o tsv)

az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee-object-id "${USER_OID}" \
  --assignee-principal-type User \
  --scope "${SA_RESOURCE_ID}" \
  -o json 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R00-role-assign.log"

# Wait for RBAC propagation (typically 60-90 seconds)
echo "Waiting 90 seconds for role assignment propagation..."
sleep 90
```

Save these values for Phase 4 and Phase 5 cleanup:
```bash
echo "${SA_RESOURCE_ID}" > "${OUTPUT_DIR}/phase-3-remote/SA_RESOURCE_ID"
echo "${USER_OID}" > "${OUTPUT_DIR}/phase-3-remote/USER_OID"
```

### Test R01: Export local state (Scenario 2)

Continue in Phase 2's `WORKDIR`:

```bash
cd "${WORKDIR}"
tlumi state pull > "${OUTPUT_DIR}/phase-3-remote/R01-local-state.json" 2>"${OUTPUT_DIR}/phase-3-remote/R01-local-state.err"
```

**Assertions**:
- Exit code is 0
- Valid JSON with resources

### Test R02: Clean local state and reconfigure backend (Scenario 2)

```bash
tlumi clean --include-state --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R02-clean.log"

# Update tlumi.yaml with remote backend
cat > tlumi.yaml << YAMLEOF
project:
  name: ${PREFIX}-test
  entry: infra.py

secrets:
  allow_unencrypted: true

backend:
  url: "azblob://tfstate?storage_account=${STATE_ACCOUNT}"

variables:
  location: ${LOCATION}
  environment: dev
  subscription_id: ${SUBSCRIPTION_ID}
YAMLEOF

# Re-init with remote backend
echo "" | tlumi init 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R02-reinit.log"
```

**Assertions**:
- Clean succeeds
- Init succeeds with remote backend

### Test R03: Push state to remote backend (Scenario 2)

```bash
tlumi state push "${OUTPUT_DIR}/phase-3-remote/R01-local-state.json" --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R03-push-remote.log"
```

**Assertions**:
- Exit code is 0

### Test R04: Plan shows no changes after migration (Scenario 2)

```bash
tlumi plan --var prefix=${PREFIX} 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R04-plan-noop.log"
```

**Assertions**:
- Exit code is 0
- Output shows 0 creates, 0 deletes (state migrated correctly)
- Note: 1 pending update for `manual-tag` removal on the ResourceGroup is expected
  drift from Phase 2 D19 (manual az tag change). This is NOT a migration issue.

### Test R05: State operations work with remote backend (Scenario 9)

```bash
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R05-state-list.log"
tlumi output --json 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R05-output.log"
```

**Assertions**:
- Both commands succeed
- State list shows resources
- Output shows expected values

### Test R06: State pull from remote backend (Scenario 9)

```bash
tlumi state pull > "${OUTPUT_DIR}/phase-3-remote/R06-remote-pull.json" 2>"${OUTPUT_DIR}/phase-3-remote/R06-remote-pull.err"
```

**Assertions**:
- Valid JSON matching the originally pushed state
- Same resources present as in R01

### Test R07: State unlock with remote backend (Scenario 12)

```bash
tlumi state unlock 2>&1 | tee "${OUTPUT_DIR}/phase-3-remote/R07-unlock.log"
UNLOCK_EXIT=$?
echo "exit_code=${UNLOCK_EXIT}" >> "${OUTPUT_DIR}/phase-3-remote/R07-unlock.log"
```

**Assertions**:
- Command completes without crash

---

## Phase 4: Auth Patterns (Scenario 17)

### Test R08: SAS token authentication for backend

Generate a SAS token and use it for backend auth:

```bash
mkdir -p "${OUTPUT_DIR}/phase-4-auth"

# Generate SAS token (valid for 1 hour)
EXPIRY=$(date -u -d "+1 hour" +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -v+1H +%Y-%m-%dT%H:%MZ)
STATE_KEY=$(az storage account keys list \
  --account-name "${STATE_ACCOUNT}" \
  --resource-group "rg-${PREFIX}-state" \
  --query "[0].value" -o tsv)

SAS_TOKEN=$(az storage container generate-sas \
  --name tfstate \
  --account-name "${STATE_ACCOUNT}" \
  --account-key "${STATE_KEY}" \
  --permissions rwdl \
  --expiry "${EXPIRY}" \
  -o tsv 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R08-sas-generate.log")

# Configure backend with SAS token via env var
export AZURE_STORAGE_SAS_TOKEN="${SAS_TOKEN}"
export AZURE_STORAGE_ACCOUNT="${STATE_ACCOUNT}"

# Clean and re-init (forces new backend connection)
tlumi clean --include-state --auto-approve 2>&1 >/dev/null

cat > tlumi.yaml << YAMLEOF
project:
  name: ${PREFIX}-test
  entry: infra.py

secrets:
  allow_unencrypted: true

backend:
  url: "azblob://tfstate"

variables:
  location: ${LOCATION}
  environment: dev
  subscription_id: ${SUBSCRIPTION_ID}
YAMLEOF

echo "" | tlumi init 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R08-sas-init.log"

# Push state using SAS auth
tlumi state push "${OUTPUT_DIR}/phase-3-remote/R01-local-state.json" --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R08-sas-push.log"

# Verify operations work
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R08-sas-state-list.log"
tlumi plan --var prefix=${PREFIX} 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R08-sas-plan.log"
```

**Assertions**:
- SAS token generated successfully
- `state push` succeeds with SAS auth
- `state list` shows resources
- `plan` shows no changes (state matches)

Clean up SAS env vars:
```bash
unset AZURE_STORAGE_SAS_TOKEN
unset AZURE_STORAGE_ACCOUNT
```

### Test R09: Azure AD authentication for backend

The Storage Blob Data Contributor role was already assigned in Phase 3 setup
(required for the azblob backend). This test verifies Azure AD auth works
explicitly after the SAS token test, with a clean re-init:


```bash
# Clean and re-init with storage_account in URL
tlumi clean --include-state --auto-approve 2>&1 >/dev/null

cat > tlumi.yaml << YAMLEOF
project:
  name: ${PREFIX}-test
  entry: infra.py

secrets:
  allow_unencrypted: true

backend:
  url: "azblob://tfstate?storage_account=${STATE_ACCOUNT}"

variables:
  location: ${LOCATION}
  environment: dev
  subscription_id: ${SUBSCRIPTION_ID}
YAMLEOF

echo "" | tlumi init 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R09-aad-init.log"

# Push state using Azure AD auth (DefaultAzureCredential picks up az login)
tlumi state push "${OUTPUT_DIR}/phase-3-remote/R01-local-state.json" --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R09-aad-push.log"

# Verify operations
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R09-aad-state-list.log"
tlumi plan --var prefix=${PREFIX} 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R09-aad-plan.log"
```

**Assertions**:
- Role assignment succeeds
- `state push` succeeds with Azure AD auth
- `state list` shows resources
- `plan` shows no changes

### Test R10: Verify SAS and Azure AD produce same state

```bash
tlumi state pull > "${OUTPUT_DIR}/phase-4-auth/R10-aad-state.json" 2>/dev/null

# Compare resource counts between SAS-pushed and AAD-pushed state
python3 -c "
import json
sas = json.load(open('${OUTPUT_DIR}/phase-3-remote/R01-local-state.json'))
aad = json.load(open('${OUTPUT_DIR}/phase-4-auth/R10-aad-state.json'))
sas_urns = {r['urn'] for r in sas.get('deployment', {}).get('resources', [])}
aad_urns = {r['urn'] for r in aad.get('deployment', {}).get('resources', [])}
print(f'SAS resources: {len(sas_urns)}')
print(f'AAD resources: {len(aad_urns)}')
print(f'Match: {sas_urns == aad_urns}')
" 2>&1 | tee "${OUTPUT_DIR}/phase-4-auth/R10-compare.log"
```

**Assertions**:
- Resource URNs match between both auth methods

---

## Phase 3-4 Summary

Phase 3 tests: 7 (R01-R07)
Phase 4 tests: 3 (R08-R10)
Total: 10

## Notes for Cleanup

Save these for Phase 5:
- `STATE_ACCOUNT` - storage account name to delete
- `rg-${PREFIX}-state` - state resource group to delete
- `WORKDIR` - project directory to clean up
- `SA_RESOURCE_ID` and `USER_OID` saved to `${OUTPUT_DIR}/phase-3-remote/` during setup
- Note: role assignment is auto-deleted when the storage account's resource group
  is deleted, so explicit role cleanup is best-effort only
