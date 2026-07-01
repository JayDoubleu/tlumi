---
name: test-azure-scenarios
description: >
  This skill should be used when the user asks to "test azure scenarios",
  "run scenario tests", "test tlumi workflows", "verify scenario parity",
  "test all scenarios", "run integration tests", "test azure workflows",
  "test tlumi against azure", or wants to verify that tlumi behaves as
  documented in docs/internal/scenarios-azure.md from a developer workstation.
version: 0.1.0
---

# Test Azure Scenarios

End-to-end test of workflow scenarios from `docs/internal/scenarios-azure.md`. Execute
every scenario that can run from a developer workstation (with `az login`),
capture all output, and report pass/fail results. The doc maps 18 Terraform
workflows to their tlumi equivalents. This skill tests all of them except CI/CD
pipeline, managed identity, and OIDC/federation scenarios.

## Scope

### Tested (runnable from a workstation)

| Phase | Scenarios | What is tested |
|-------|-----------|----------------|
| 1: Local-only | 3 (local), 15, 18 | init, fmt, validate, clean, variables, project structure |
| 2: Deploy (local state) | 1, 3 (azure), 5, 6, 7, 8, 9, 10, 13, 14 | plan, apply, update, replace, target, import, state ops, outputs, refresh |
| 3: Remote state | 2, 12 | state migration, remote backend, locked state |
| 4: Auth patterns | 17 (SAS + Azure AD) | SAS token auth, Azure AD auth with role assignment |
| 5: Destroy & cleanup | 11, 15 | destroy, targeted destroy, clean, Azure resource teardown |

### Not tested (requires infrastructure not available on a workstation)

| Scenario | Reason |
|----------|--------|
| 4: CI/CD with Azure AD | Requires GitHub Actions pipeline with OIDC |
| 16: Multi-environment | Scenario 18 covers the local structure; full multi-env needs separate state backends |
| 17: OIDC / Federation | Requires federated credential in Entra ID |
| 17: Managed Identity | Requires Azure VM / AKS with assigned identity |

## Prerequisites

### Gather User Configuration

Ask the user the following questions before starting:

**Question 1** (required):
- "Which Azure subscription should be used for testing?"
- Options: Let the user provide subscription ID or name
- Store as `SUBSCRIPTION_ID`

**Question 2** (with defaults):
- "Which Azure region and resource naming prefix to use?"
- Default region: `westeurope`
- Default prefix: `tlumitst` (must be short, lowercase, no hyphens for storage account compatibility)
- Store as `LOCATION` and `PREFIX`

**Question 3** (optional phases):
- "Which test phases to run?"
- Options: "All phases (Recommended)", "Local-only (no Azure)", "Deploy + local state only", "Include remote state + auth patterns"
- Determines which phases execute

### Verify Tools

After gathering configuration, verify prerequisites:

```bash
# Azure CLI installed and logged in
az version
az account show --query "id" -o tsv
az account set --subscription "${SUBSCRIPTION_ID}"

# tlumi installed (install from repo if needed)
which tlumi || pip install -e "${REPO_ROOT}"
tlumi version
```

Fail immediately with clear error if `az` is not installed or not logged in.

## Execution

### Output Directory

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
SKILL_DIR="${REPO_ROOT}/.claude/skills/test-azure-scenarios"
SYNTHETIC_STATE="${SKILL_DIR}/assets/synthetic-state.json"
TIMESTAMP=$(date +%Y-%m-%dT%H-%M-%S)
OUTPUT_DIR="${REPO_ROOT}/.scenarios/azure-scenarios/${TIMESTAMP}"
mkdir -p "${OUTPUT_DIR}"
```

Notify the user: "Test outputs will be saved to `.scenarios/azure-scenarios/<timestamp>/`"

Verify `.scenarios/` is in `.gitignore`. Append if missing.

### Naming Convention

All Azure resources use the prefix:
- Resource groups: `rg-${PREFIX}-test`, `rg-${PREFIX}-state`
- Storage accounts: `st${PREFIX}state` (no hyphens, max 24 chars)
- Blob container: `tfstate`

### Phase Execution

Execute phases sequentially. Each phase's detailed test plan is in its reference
file. Read the reference file before executing each phase.

**Phase 1: Local-only tests** (no Azure resources needed)
- Reference: **`references/phase-local.md`**
- Tests: init, fmt, validate, clean, variables, project structure
- Creates temp directories, runs tests, cleans up
- No Azure dependencies; may optionally run in parallel with Phase 2 setup

**Phase 2: Deploy with local state** (creates Azure resources)
- Reference: **`references/phase-deploy.md`**
- Setup: Create test project with infra.py that deploys a resource group + storage account
- Tests: plan, apply, update, replace, target, import, state ops, outputs, refresh
- Leaves resources deployed for Phase 3

**Phase 3: Remote state** (migrates state to Azure Blob Storage)
- Reference: **`references/phase-remote-state.md`**
- Setup: Create storage account for state via `az` CLI
- Tests: state migration (pull/clean/push), remote operations, locked state
- Conditional: skip if user chose local-only

**Phase 4: Auth patterns** (tests SAS token and Azure AD auth)
- Reference: **`references/phase-remote-state.md`** (auth section)
- Tests: SAS token auth for backend, Azure AD auth with role assignment
- Conditional: skip if user chose not to test auth patterns
- Role assignment: Storage Blob Data Contributor on the state storage account

**Phase 5: Destroy & cleanup** (tears down everything)
- Reference: **`references/phase-cleanup.md`**
- Tests: `tlumi destroy`, targeted destroy, `tlumi clean`
- Cleanup: Delete ALL test Azure resources via `az group delete`
- Always runs (even if earlier phases fail)

### Log File Format

Every log file MUST start with a metadata header before the command output.
Define this helper function at the start of each phase and call it before
every `tee`:

```bash
_log_header() {
  local logfile="$1"
  local cmd="$2"
  printf "# CWD: %s\n# CMD: %s\n# TS: %s\n---\n" "$(pwd)" "${cmd}" "$(date -Iseconds)" > "${logfile}"
}
```

Usage pattern (note `tee -a` to append after the header):

```bash
_log_header "${OUTPUT_DIR}/phase-1-local/L01-init.log" "echo '' | tlumi init"
echo "" | tlumi init 2>&1 | tee -a "${OUTPUT_DIR}/phase-1-local/L01-init.log"
```

This makes each log self-documenting. For `state pull` with separate stderr,
write the header first, then redirect:

```bash
_log_header "${OUTPUT_DIR}/phase-1-local/L16-state-pull.json" "tlumi state pull"
tlumi state pull >> "${OUTPUT_DIR}/phase-1-local/L16-state-pull.json" 2>"${OUTPUT_DIR}/phase-1-local/L16-state-pull.err"
```

### Test Tracking

Track every test case with ID, name, and PASS/FAIL result. Each phase's
reference file defines the test IDs. After each command, verify assertions
and record the result. Capture full command output to log files.

### Compile Results

After all phases complete, write `${OUTPUT_DIR}/summary.md`:

```markdown
# Azure Scenario Test Results
Date: <timestamp>
tlumi version: <version>
Subscription: <id>
Region: <location>
Prefix: <prefix>

## Results

| ID | Phase | Scenario | Test | Result |
|----|-------|----------|------|--------|
| L01 | 1 | 1 | tlumi init creates scaffold | PASS |
| ... | | | | |

## Summary: Passed N/M
## Phases: 1 (PASS), 2 (PASS), 3 (SKIP), ...

## Failure Details
(full command output for any failures)
```

Report the summary table to the user.

## Synthetic State

The file `assets/synthetic-state.json` is a **resource template**, not a
directly pushable state file. Pulumi validates a magic cookie and
secrets_providers in the state envelope, so pushing the raw template fails
with "magic cookie mismatch".

**Correct approach** (implemented in Phase 1 Group C setup):

1. Initialize a real project with `allow_unencrypted: true`
2. Pull the empty (valid) state: `tlumi state pull` (contains correct magic cookie)
3. Inject synthetic resources from the template into the pulled state using Python
4. Write the merged state to a temp file and push it: `tlumi state push`

See `references/phase-local.md` Group C Setup for the exact Python merge script.

The template defines these resources:

| Resource | Type | Name | Key Details |
|----------|------|------|-------------|
| Stack | `pulumi:pulumi:Stack` | test-project-default | Outputs: `resource_group: "rg-dev"` |
| Provider | `pulumi:providers:azure-native` | default | Version 2.0.0 |
| ResourceGroup | `azure-native:resources:ResourceGroup` | rg | Location: westeurope |
| StorageAccount | `azure-native:storage:StorageAccount` | storage | Depends on rg |

The dependency chain is: `storage` depends on `rg`. This enables testing of
`state rm` reference cleanup and `state mv` reference rewriting.

## Error Handling

- If a test fails, log it and continue with remaining tests in the phase
- If Azure resource creation fails, skip dependent phases and jump to cleanup
- Phase 5 (cleanup) always runs regardless of earlier failures
- Wrap all Azure resource creation in try/cleanup patterns
- Capture stderr alongside stdout for all commands
- For `state pull`, separate stdout from stderr to keep JSON clean

## Additional Resources

### Reference Files

- **`references/phase-local.md`** - Phase 1: 29 local-only test cases covering
  init, fmt, validate, clean, variable handling, and project structure validation.
  No Azure credentials needed.

- **`references/phase-deploy.md`** - Phase 2: 24 test cases for deploying real
  Azure resources with local state. Covers plan, apply, update, replace, target,
  import, state operations, outputs, and refresh. Includes the infra.py code and
  tlumi.yaml configuration used for testing.

- **`references/phase-remote-state.md`** - Phases 3-4: 10 test cases for remote
  state migration, SAS token auth, and Azure AD auth. Includes Azure CLI commands
  for creating state storage infrastructure and role assignments.

- **`references/phase-cleanup.md`** - Phase 5: destroy tests and Azure resource
  teardown. Includes both tlumi destroy and `az group delete` commands to ensure
  complete cleanup.

### Assets

- **`assets/synthetic-state.json`** - Resource template for Phase 1 offline state
  tests. NOT directly pushable (Pulumi rejects the magic cookie). Must be merged
  into a real empty state via the pull-inject-push pattern described in the
  "Synthetic State" section above. Contains a Stack with outputs, a provider, a
  ResourceGroup, and a StorageAccount with dependency relationships.
