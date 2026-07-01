# Phase 2: Deploy with Local State

Deploy real Azure resources using tlumi with local state backend. Tests the full
plan/apply/update/replace/target/import/state/output/refresh workflow.

Reference scenarios: 1, 3 (azure), 5, 6, 7, 8, 9, 10, 13, 14

**Prerequisites**: Azure CLI logged in, subscription set, `SUBSCRIPTION_ID`,
`LOCATION`, and `PREFIX` variables available.

---

## Setup: Create Test Project

```bash
WORKDIR=$(mktemp -d)
cd "${WORKDIR}"
mkdir -p "${OUTPUT_DIR}/phase-2-deploy"

# Define log header function (MUST be called before every tee)
_log_header() {
  local logfile="$1"
  local cmd="$2"
  printf "# CWD: %s\n# CMD: %s\n# TS: %s\n---\n" "$(pwd)" "${cmd}" "$(date -Iseconds)" > "${logfile}"
}

_log_header "${OUTPUT_DIR}/phase-2-deploy/D00-init.log" "echo '' | tlumi init"
echo "" | tlumi init 2>&1 | tee -a "${OUTPUT_DIR}/phase-2-deploy/D00-init.log"
```

Write `tlumi.yaml`:

```bash
cat > tlumi.yaml << YAMLEOF
project:
  name: ${PREFIX}-test
  entry: infra.py

secrets:
  allow_unencrypted: true

variables:
  location: ${LOCATION}
  environment: dev
  subscription_id: ${SUBSCRIPTION_ID}
YAMLEOF
```

Write `infra.py` (creates a resource group + storage account):

```bash
cat > infra.py << 'PYEOF'
import pulumi
from pulumi_azure_native import resources, storage

config = pulumi.Config()
location = config.require("location")
environment = config.require("environment")
subscription_id = config.require("subscription_id")
prefix = config.get("prefix") or "tlumitst"

# Resource Group
rg = resources.ResourceGroup(
    "rg",
    resource_group_name=f"rg-{prefix}-{environment}",
    location=location,
    tags={"managed-by": "tlumi", "environment": environment},
)

# Storage Account
sa = storage.StorageAccount(
    "storage",
    resource_group_name=rg.name,
    location=location,
    account_name=f"st{prefix}{environment}",
    kind=storage.Kind.STORAGE_V2,
    sku=storage.SkuArgs(name=storage.SkuName.STANDARD_LRS),
    tags={"managed-by": "tlumi"},
)

pulumi.export("resource_group", rg.name)
pulumi.export("storage_account", sa.name)
pulumi.export("primary_endpoint", sa.primary_endpoints.blob)
PYEOF
```

Add the Azure Native provider:

```bash
tlumi deps add pulumi-azure-native 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D00-deps-add.log"
```

---

**NOTE**: All tests below MUST use `_log_header` before `tee -a` on every log
file. The first test shows the full pattern; subsequent tests show the command
only for brevity but the header is always required.

## Test D01: tlumi deps list shows installed packages (Scenario 3)

```bash
_log_header "${OUTPUT_DIR}/phase-2-deploy/D01-deps-list.log" "tlumi deps list"
tlumi deps list 2>&1 | tee -a "${OUTPUT_DIR}/phase-2-deploy/D01-deps-list.log"
```

**Assertions**:
- Exit code is 0
- Output contains `pulumi-azure-native`

## Test D02: tlumi plan shows creates (Scenario 1)

```bash
tlumi plan --var prefix=${PREFIX} 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D02-plan.log"
```

**Assertions**:
- Exit code is 0
- Output shows `+` (create) operations for ResourceGroup and StorageAccount
- Output shows resource names containing the prefix

## Test D03: tlumi plan --json (Scenario 1)

```bash
tlumi plan --var prefix=${PREFIX} --json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D03-plan-json.log"
```

**Assertions**:
- Exit code is 0
- Output is valid JSON
- JSON contains `create` count > 0

## Test D04: tlumi apply deploys resources (Scenario 1)

```bash
tlumi apply --var prefix=${PREFIX} --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D04-apply.log"
```

**Assertions**:
- Exit code is 0
- Output shows resources created successfully
- Output shows summary with creates > 0

## Test D05: Verify resources exist in Azure (Scenario 1)

```bash
az group show --name "rg-${PREFIX}-dev" -o json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D05-az-verify-rg.log"
az storage account show --name "st${PREFIX}dev" --resource-group "rg-${PREFIX}-dev" -o json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D05-az-verify-sa.log"
```

**Assertions**:
- Both `az` commands succeed (exit code 0)
- Resource group exists in the correct location
- Storage account exists in the correct resource group

## Test D06: tlumi plan shows no changes (Scenario 5 baseline)

```bash
tlumi plan --var prefix=${PREFIX} 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D06-plan-noop.log"
```

**Assertions**:
- Exit code is 0
- Output indicates no changes (0 creates, 0 updates, 0 deletes)

## Test D07: Modify infra, plan shows update (Scenario 5)

Modify the resource group tags to trigger an in-place update:

```bash
sed -i 's/"environment": environment/"environment": environment, "version": "2"/' infra.py
tlumi plan --var prefix=${PREFIX} 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D07-plan-update.log"
```

**Assertions**:
- Exit code is 0
- Output shows `~` (update) for the ResourceGroup
- Output shows property diff with the new `version` tag

## Test D08: tlumi apply applies the update (Scenario 5)

```bash
tlumi apply --var prefix=${PREFIX} --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D08-apply-update.log"
```

**Assertions**:
- Exit code is 0
- Output shows resources updated

## Test D09: Verify update in Azure (Scenario 5)

```bash
az group show --name "rg-${PREFIX}-dev" --query "tags" -o json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D09-az-verify-tags.log"
```

**Assertions**:
- Tags include `version: "2"`

## Test D10: tlumi plan -t rg targets specific resource (Scenario 7)

```bash
tlumi plan --var prefix=${PREFIX} -t rg 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D10-plan-target.log"
```

**Assertions**:
- Exit code is 0
- Output mentions targeting
- Plan only shows the resource group, not the storage account

## Test D11: tlumi plan --replace rg shows replacement (Scenario 6)

```bash
tlumi plan --var prefix=${PREFIX} --replace rg 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D11-plan-replace.log"
```

**Assertions**:
- Exit code is 0
- Output shows replacement operation for ResourceGroup

## Test D12: tlumi state list shows deployed resources (Scenario 9)

```bash
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D12-state-list.log"
```

**Assertions**:
- Exit code is 0
- Output lists ResourceGroup (`rg`) and StorageAccount (`storage`)

## Test D13: tlumi state show rg (Scenario 9)

```bash
tlumi state show rg 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D13-state-show.log"
```

**Assertions**:
- Exit code is 0
- Output shows resource group details with real Azure resource ID
- Output contains the location and tags

## Test D14: tlumi show --json (Scenario 9)

```bash
tlumi show --json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D14-show-json.log"
```

**Assertions**:
- Exit code is 0
- Valid JSON with `version` and `deployment` keys
- Contains real Azure resource IDs

## Test D15: tlumi state pull (Scenario 9)

```bash
tlumi state pull > "${OUTPUT_DIR}/phase-2-deploy/D15-state-pull.json" 2>"${OUTPUT_DIR}/phase-2-deploy/D15-state-pull.err"
```

**Assertions**:
- Exit code is 0
- Valid JSON with `version` and `deployment` keys

## Test D16: tlumi output shows exports (Scenario 13)

```bash
tlumi output 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D16-output.log"
```

**Assertions**:
- Exit code is 0
- Shows `resource_group`, `storage_account`, `primary_endpoint`
- Values are real Azure resource names/URLs

## Test D17: tlumi output --json (Scenario 13)

```bash
tlumi output --json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D17-output-json.log"
```

**Assertions**:
- Exit code is 0
- Valid JSON with all three output keys

## Test D18: tlumi output resource_group --raw (Scenario 13)

```bash
RG_NAME=$(tlumi output resource_group --raw 2>/dev/null)
echo "raw_output=${RG_NAME}" > "${OUTPUT_DIR}/phase-2-deploy/D18-output-raw.log"
```

**Assertions**:
- Exit code is 0
- Value equals `rg-${PREFIX}-dev` (exact match, no Rich formatting)

## Test D19: Refresh after manual change (Scenario 14)

Add a tag manually via Azure CLI, then refresh:

```bash
az group update --name "rg-${PREFIX}-dev" --tags managed-by=tlumi environment=dev version=2 manual-tag=true -o json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D19-az-manual-change.log"

tlumi refresh --var prefix=${PREFIX} --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D19-refresh.log"
```

**Assertions**:
- `az group update` succeeds
- `tlumi refresh` exit code is 0
- Refresh output shows the ResourceGroup was updated (state now includes `manual-tag`)

## Test D20: Import existing resource (Scenario 8)

Create a resource manually, then import it into tlumi:

```bash
# Create a second resource group manually
az group create --name "rg-${PREFIX}-imported" --location "${LOCATION}" --tags imported=true -o json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D20-az-create-import-target.log"

# Get the full resource ID
IMPORT_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/rg-${PREFIX}-imported"

# Add the resource to infra.py (heredoc unquoted intentionally for variable interpolation)
cat >> infra.py << PYEOF

# Imported resource
imported_rg = resources.ResourceGroup(
    "imported-rg",
    resource_group_name="rg-${PREFIX}-imported",
    location="${LOCATION}",
    tags={"imported": "true"},
)
pulumi.export("imported_rg", imported_rg.name)
PYEOF

# Import it
tlumi import azure-native:resources:ResourceGroup imported-rg "${IMPORT_ID}" --var prefix=${PREFIX} 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D20-import.log"
```

**Assertions**:
- Exit code is 0
- `tlumi state list` now includes `imported-rg`
- `tlumi plan --var prefix=${PREFIX}` shows no changes for the imported resource (or minimal drift)

## Test D21: tlumi state mv renames resource (Scenario 10)

```bash
tlumi state mv imported-rg renamed-rg --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D21-state-mv.log"
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D21-after-mv-list.log"
```

**Assertions**:
- Exit code is 0
- Backup created in `.tlumi/backups/`
- `state list` shows `renamed-rg` instead of `imported-rg`

## Test D22: tlumi state rm removes resource from state (Scenario 9)

Remove the imported/renamed resource from state (without destroying it):

```bash
tlumi state rm renamed-rg --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D22-state-rm.log"
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D22-after-rm-list.log"
```

**Assertions**:
- Exit code is 0
- `state list` no longer shows `renamed-rg`
- Resource still exists in Azure: `az group show --name "rg-${PREFIX}-imported"` succeeds

Also remove the imported resource definition from infra.py (revert the append):

```bash
# Remove the imported resource block from infra.py
python3 -c "
content = open('infra.py').read()
marker = '# Imported resource'
idx = content.find(marker)
if idx > 0:
    open('infra.py', 'w').write(content[:idx].rstrip() + '\n')
"
```

Clean up the manually created resource group:

```bash
az group delete --name "rg-${PREFIX}-imported" --yes --no-wait 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D22-az-cleanup-imported.log"
```

## Test D23: tlumi plan --destroy shows destruction preview (Scenario 11)

```bash
tlumi plan --destroy --var prefix=${PREFIX} 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D23-plan-destroy.log"
```

**Assertions**:
- Exit code is 0
- Output shows `-` (delete) operations for all resources

## Test D24: tlumi plan --destroy --json (Scenario 11)

```bash
tlumi plan --destroy --var prefix=${PREFIX} --json 2>&1 | tee "${OUTPUT_DIR}/phase-2-deploy/D24-plan-destroy-json.log"
```

**Assertions**:
- Exit code is 0
- Valid JSON with `delete` count > 0

---

## Phase 2 Notes

- Do NOT destroy resources yet (Phase 3 uses them for state migration)
- Save `WORKDIR` path for Phase 3 to continue from
- Save pulled state path for Phase 3 migration
- If Phase 3 is skipped, proceed directly to Phase 5 (cleanup)

## Phase 2 Summary

Total tests: 24 (D01-D24)
