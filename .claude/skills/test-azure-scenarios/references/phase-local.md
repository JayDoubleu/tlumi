# Phase 1: Local-Only Tests

No Azure credentials needed. Tests init, format, validate, clean, and variable
handling. All tests run in isolated temp directories.

Reference scenarios: 1 (init part), 3 (local parts), 15, 18

---

## Setup

```bash
mkdir -p "${OUTPUT_DIR}/phase-1-local"

# Define log header function (MUST be called before every tee)
_log_header() {
  local logfile="$1"
  local cmd="$2"
  printf "# CWD: %s\n# CMD: %s\n# TS: %s\n---\n" "$(pwd)" "${cmd}" "$(date -Iseconds)" > "${logfile}"
}
```

---

**NOTE**: All tests below MUST use `_log_header` before `tee -a` on every log
file. The first test in each group shows the full pattern; subsequent tests
show the command only for brevity but the header is always required.

## Group A: Init & Lifecycle (Scenarios 1, 15)

### Test L01: tlumi init creates scaffold (Scenario 1)

**Note**: `mktemp -d` creates directories like `tmp.XXXXXXXX` which fail tlumi's
project name validation (must start with a letter). Create a named subdirectory
inside the temp dir for test projects that need `tlumi init`:

```bash
TMPDIR=$(mktemp -d)
WORKDIR="${TMPDIR}/test-project"
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"
_log_header "${OUTPUT_DIR}/phase-1-local/L01-init.log" "echo '' | tlumi init"
echo "" | tlumi init 2>&1 | tee -a "${OUTPUT_DIR}/phase-1-local/L01-init.log"
```

Press Enter at passphrase prompt (no passphrase).

**Assertions**:
- Exit code is 0
- `tlumi.yaml` exists and contains `project:` with `name:` and `entry:` keys
- `infra.py` exists and compiles without syntax error (`python3 -c "compile(open('infra.py').read(), 'infra.py', 'exec')"`)
- `requirements.txt` exists
- `.tlumi/` directory exists
- `.gitignore` exists and contains `.tlumi/`
- `.tlumi/` directory permissions are 0o700 (`stat -c '%a' .tlumi` outputs `700`)

### Test L02: tlumi clean preserves state (Scenario 15)

Create a fake state file to verify preservation:

```bash
mkdir -p .tlumi/state .tlumi/cache/venv
echo '{}' > .tlumi/state/fake-state
echo 'cached' > .tlumi/cache/venv/marker
tlumi clean --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L02-clean-preserve.log"
```

**Assertions**:
- Exit code is 0
- `.tlumi/state/fake-state` still exists (state preserved)
- `.tlumi/cache/venv/marker` does NOT exist (caches cleaned)

### Test L03: tlumi clean --include-state removes everything (Scenario 15)

```bash
echo "" | tlumi init 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L03-reinit.log"
tlumi clean --include-state --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L03-clean-all.log"
```

**Assertions**:
- Exit code is 0
- `.tlumi/` directory does NOT exist
- `tlumi.yaml` still exists (clean only removes .tlumi/)

### Test L04: Re-init after clean (Scenario 15)

```bash
echo "" | tlumi init 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L04-reinit.log"
```

**Assertions**:
- Exit code is 0
- `.tlumi/` directory exists again
- All scaffold files present (tlumi.yaml, infra.py, requirements.txt, .gitignore)

**Cleanup**: `rm -rf "${TMPDIR}"`

---

## Group B: Format & Validate (Scenario 3, local parts)

### Test L05: tlumi fmt formats Python files

```bash
TMPDIR=$(mktemp -d)
WORKDIR="${TMPDIR}/test-fmt"
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"
echo "" | tlumi init 2>&1 >/dev/null

# Write intentionally unformatted infra.py
cat > infra.py << 'PYEOF'
import   pulumi
x=1
y =   2
def   foo( ):
    return   x+y
PYEOF

cp infra.py "${OUTPUT_DIR}/phase-1-local/L05-before.py"
tlumi fmt 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L05-fmt.log"
cp infra.py "${OUTPUT_DIR}/phase-1-local/L05-after.py"
```

**Assertions**:
- Exit code is 0
- `infra.py` content changed (diff between before and after is non-empty)
- Reformatted file has `x = 1` (not `x=1`)

### Test L06: tlumi fmt --check detects unformatted code

```bash
cat > infra.py << 'PYEOF'
x=1
PYEOF

tlumi fmt --check 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L06-fmt-check-fail.log"
FMT_EXIT=$?
echo "exit_code=${FMT_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L06-fmt-check-fail.log"
```

**Assertions**:
- Exit code is NON-ZERO

### Test L07: tlumi fmt --check passes on formatted code

```bash
tlumi fmt 2>&1 >/dev/null  # format first
tlumi fmt --check 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L07-fmt-check-pass.log"
FMT_EXIT=$?
echo "exit_code=${FMT_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L07-fmt-check-pass.log"
```

**Assertions**:
- Exit code is 0

### Test L08: tlumi validate with valid config

```bash
# Restore valid infra.py
cat > infra.py << 'PYEOF'
import pulumi
pulumi.export("test", "value")
PYEOF

tlumi validate 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L08-validate.log"
```

**Assertions**:
- Exit code is 0
- Output contains success indication

### Test L09: tlumi validate --json

```bash
tlumi validate --json 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L09-validate-json.log"
```

**Assertions**:
- Exit code is 0
- Output is valid JSON (parse with `python3 -c "import json,sys; json.load(sys.stdin)"`)

### Test L10: tlumi validate catches syntax errors

```bash
cat > infra.py << 'PYEOF'
def broken(
    # missing closing paren
PYEOF

tlumi validate 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L10-validate-syntax.log"
VAL_EXIT=$?
echo "exit_code=${VAL_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L10-validate-syntax.log"
```

**Assertions**:
- Exit code is NON-ZERO
- Output mentions syntax error

### Test L11: tlumi validate catches bad project name

```bash
cat > tlumi.yaml << 'YAMLEOF'
project:
  name: "123-bad-start"
  entry: infra.py
YAMLEOF

tlumi validate 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L11-validate-bad-name.log"
VAL_EXIT=$?
echo "exit_code=${VAL_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L11-validate-bad-name.log"
```

**Assertions**:
- Exit code is NON-ZERO
- Output indicates name validation failure

**Cleanup**: `rm -rf "${TMPDIR}"`

---

## Group C: State Operations with Synthetic State (Scenarios 9, 10)

These tests use synthetic resources to test state commands without real Azure
resources. The `assets/synthetic-state.json` is a resource template that must
be merged into a real Pulumi state (Pulumi rejects raw templates due to magic
cookie validation).

### Setup

```bash
TMPDIR=$(mktemp -d)
WORKDIR="${TMPDIR}/test-project"
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"
echo "" | tlumi init 2>&1 >/dev/null

cat > tlumi.yaml << 'YAMLEOF'
project:
  name: test-project
  entry: infra.py

secrets:
  allow_unencrypted: true
YAMLEOF

cat > infra.py << 'PYEOF'
import pulumi
pulumi.export("resource_group", "rg-dev")
PYEOF
```

Pull the empty (valid) state, inject synthetic resources, and push:

```bash
# Pull the real empty state (has correct magic cookie + secrets_providers)
EMPTY_STATE=$(tlumi state pull 2>/dev/null)

# Merge synthetic resources into the real state envelope
python3 << PYEOF
import json

real = json.loads('''${EMPTY_STATE}''')
template = json.load(open("${SYNTHETIC_STATE}"))

# Replace resources from template into real state envelope
real["deployment"]["resources"] = template["deployment"]["resources"]
json.dump(real, open("/tmp/tlumi-merged-state.json", "w"), indent=2)
PYEOF

_log_header "${OUTPUT_DIR}/phase-1-local/C00-setup.log" "tlumi state push (merged synthetic state)"
tlumi state push /tmp/tlumi-merged-state.json --auto-approve 2>&1 | tee -a "${OUTPUT_DIR}/phase-1-local/C00-setup.log"
rm -f /tmp/tlumi-merged-state.json
```

### Test L12: tlumi state list (Scenario 9)

```bash
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L12-state-list.log"
```

**Assertions**:
- Exit code is 0
- Output contains `ResourceGroup` and `rg`
- Output contains `StorageAccount` and `storage`

### Test L13: tlumi state list --json (Scenario 9)

```bash
tlumi state list --json 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L13-state-list-json.log"
```

**Assertions**:
- Exit code is 0
- Output is valid JSON
- JSON contains resource entries with `type`, `name`, and `id` fields (name is derived from URN, not the raw URN string)

### Test L14: tlumi state show rg (Scenario 9)

```bash
tlumi state show rg 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L14-state-show.log"
```

**Assertions**:
- Exit code is 0
- Output contains resource group details (type, name, inputs, outputs)
- Output contains `westeurope`

### Test L15: tlumi show --json (Scenario 9)

```bash
tlumi show --json 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L15-show-json.log"
```

**Assertions**:
- Exit code is 0
- Output is valid JSON
- JSON has `version` and `deployment` keys

### Test L16: tlumi state pull (Scenario 9)

```bash
tlumi state pull > "${OUTPUT_DIR}/phase-1-local/L16-state-pull.json" 2>"${OUTPUT_DIR}/phase-1-local/L16-state-pull.err"
```

**Assertions**:
- Exit code is 0
- Output file is valid JSON
- JSON has `version` and `deployment` keys
- `deployment.resources` contains the expected resources

### Test L17: tlumi output (Scenario 13)

```bash
tlumi output 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L17-output.log"
```

**Assertions**:
- Exit code is 0
- Output shows `resource_group` with value `rg-dev`

### Test L18: tlumi output --json (Scenario 13)

```bash
tlumi output --json 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L18-output-json.log"
```

**Assertions**:
- Exit code is 0
- Output is valid JSON
- JSON contains `resource_group` key

### Test L19: tlumi output resource_group --raw (Scenario 13)

```bash
tlumi output resource_group --raw 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L19-output-raw.log"
```

**Assertions**:
- Exit code is 0
- Output is exactly `rg-dev` (no extra formatting)

### Test L20: tlumi state rm storage (Scenario 9)

```bash
tlumi state rm storage --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L20-state-rm.log"
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L20-after-list.log"
```

**Assertions**:
- Exit code is 0
- Backup created in `.tlumi/backups/`
- `state list` no longer shows `storage`
- `state list` still shows `rg`

### Test L21: tlumi state push restores state (Scenario 9)

```bash
tlumi state push "${OUTPUT_DIR}/phase-1-local/L16-state-pull.json" --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L21-state-push.log"
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L21-after-push-list.log"
```

**Assertions**:
- Exit code is 0
- Output shows diff summary (added/removed/unchanged)
- `state list` shows both `rg` and `storage` again

### Test L22: tlumi state mv rg primary (Scenario 10)

The synthetic state's storage account has its `parent` field set to the Stack (not to `rg`), so `rg` has no children and the rename is allowed. `state mv` now rejects renaming any resource that has children (see L22b), but `rg` is safe here because its inbound link from storage is via `dependencies`, not `parent`.

```bash
tlumi state mv rg primary --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L22-state-mv.log"
tlumi state list 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L22-after-mv-list.log"
tlumi state pull > "${OUTPUT_DIR}/phase-1-local/L22-after-mv-state.json" 2>/dev/null
```

**Assertions**:
- Exit code is 0
- Backup created in `.tlumi/backups/`
- `state list` shows `primary` instead of `rg`
- In pulled state JSON: resource URN contains `::primary` (not `::rg`)
- In pulled state JSON: storage dependencies reference the new URN with `primary`

### Test L22b: tlumi state mv rejects resources with children

Build a tiny synthetic state where the storage account has `parent` pointing at the resource group (not the Stack). `state mv rg` must refuse rather than corrupt child URNs.

```bash
python3 <<'PY' > "${TMPDIR}/state-with-parent.json"
import json, sys
state = json.load(open("${SYNTHETIC_STATE}".replace("${SYNTHETIC_STATE}", "${SKILL_DIR}/assets/synthetic-state.json")))
rg_urn = "urn:pulumi:default::test-project::azure-native:resources:ResourceGroup::rg"
for r in state["deployment"]["resources"]:
    if r.get("type") == "azure-native:storage:StorageAccount":
        r["parent"] = rg_urn
json.dump(state, sys.stdout)
PY
tlumi state push "${TMPDIR}/state-with-parent.json" --auto-approve >/dev/null 2>&1
set +e
tlumi state mv rg primary --auto-approve 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L22b-state-mv-rejected.log"
MV_EXIT=$?
set -e
echo "exit_code=${MV_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L22b-state-mv-rejected.log"
```

**Assertions**:
- Exit code is non-zero
- Output mentions "child resource"
- State is unchanged (storage still references the original `rg` URN)

### Test L23: tlumi state unlock (Scenario 12)

`state unlock` runs with `runtime=False`, so it works even when `infra.py` is broken or the venv is missing.

```bash
tlumi state unlock 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L23-state-unlock.log"
UNLOCK_EXIT=$?
echo "exit_code=${UNLOCK_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L23-state-unlock.log"
```

**Assertions**:
- Exit code is 0 (no active lock to release is the expected case here)

**Cleanup**: `rm -rf "${TMPDIR}"`

---

## Group D: Variables & Structure (Scenario 18)

### Setup

```bash
TMPDIR=$(mktemp -d)
WORKDIR="${TMPDIR}/test-vars"
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"
echo "" | tlumi init 2>&1 >/dev/null

cat > tlumi.yaml << 'YAMLEOF'
project:
  name: test-vars
  entry: infra.py

variables:
  environment: dev
  location: westeurope
YAMLEOF

cat > infra.py << 'PYEOF'
import pulumi
config = pulumi.Config()
pulumi.export("env", config.get("environment") or "default")
pulumi.export("loc", config.get("location") or "default")
PYEOF
```

### Test L24: --var-file overrides tlumi.yaml variables

```bash
cat > override.yaml << 'YAMLEOF'
environment: staging
YAMLEOF

tlumi validate --var-file override.yaml 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L24-varfile.log"
```

**Assertions**:
- Exit code is 0

### Test L25: --var overrides --var-file

```bash
tlumi validate --var-file override.yaml --var environment=production 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L25-var-override.log"
```

**Assertions**:
- Exit code is 0

### Test L26: TLUMI_VAR_* environment variables

```bash
TLUMI_VAR_ENVIRONMENT=fromenv tlumi validate 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L26-env-var.log"
```

**Assertions**:
- Exit code is 0

### Test L27: Variable with colon in key rejected

```bash
cat > tlumi.yaml << 'YAMLEOF'
project:
  name: test-vars
  entry: infra.py

variables:
  "aws:region": "us-east-1"
YAMLEOF

tlumi validate 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L27-colon-key.log"
VAL_EXIT=$?
echo "exit_code=${VAL_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L27-colon-key.log"
```

**Assertions**:
- Exit code is NON-ZERO
- Output indicates colon in variable key is rejected

### Test L28: Entry path traversal rejected

```bash
cat > tlumi.yaml << 'YAMLEOF'
project:
  name: test-vars
  entry: "../../../etc/passwd"
YAMLEOF

tlumi validate 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L28-path-traversal.log"
VAL_EXIT=$?
echo "exit_code=${VAL_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L28-path-traversal.log"
```

**Assertions**:
- Exit code is NON-ZERO

### Test L29: Non-scalar variable rejected

```bash
cat > tlumi.yaml << 'YAMLEOF'
project:
  name: test-vars
  entry: infra.py

variables:
  environment: dev
  tags:
    - a
    - b
YAMLEOF

tlumi validate 2>&1 | tee "${OUTPUT_DIR}/phase-1-local/L29-non-scalar.log"
VAL_EXIT=$?
echo "exit_code=${VAL_EXIT}" >> "${OUTPUT_DIR}/phase-1-local/L29-non-scalar.log"
```

**Assertions**:
- Exit code is NON-ZERO
- Output indicates variable values must be scalars

**Cleanup**: `rm -rf "${TMPDIR}"`

---

## Phase 1 Summary

Total tests: 29 (L01-L29)
- Group A (Lifecycle): L01-L04
- Group B (Format & Validate): L05-L11
- Group C (State Operations): L12-L23
- Group D (Variables & Structure): L24-L29
