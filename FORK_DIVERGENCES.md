# Fork Divergences — IoniqPilot (`Dom` vs `StarPilotHQ/StarPilot` `Dom`)

> Maintained for `MarkenJaden/workflows` automated upstream sync (`@main`).
> Custom 2016 Hyundai Ioniq Hybrid implementations in this repository MUST be preserved across upstream merges and are NOT pushed upstream to `StarPilotHQ/StarPilot`.

## Preserved Fork Features

1. **Upstream Sync Master Workflow**
   - `.github/workflows/upstream-sync.yml` references `MarkenJaden/workflows/.github/workflows/upstream-sync.yml@main` with `workflow_sync_policy: preserve_if_no_pat`.
   - `FORK_DIVERGENCES.md` documents fork-owned paths and invariants.

2. **2016 Hyundai Ioniq Hybrid (`HYUNDAI_IONIQ` / AE HEV) Custom Suite**
   - `openpilot/starpilot/ioniq/` (`can_signals.py`, `dtc_catalog.py`, `uds_dtc.py`, `recorder.py`, `button_learner.py`, `fuel_tracker.py`, `runtime.py`, `storage.py`, `tests/`):
     - Always-on passive CAN fault monitoring + startup `ELM327` ISO-TP UDS/OBD-II ECU DTC scan & clear (`0x7E0..0x7E4`, `0x7D0..0x7D4`, `0x7C4`, `0x7C6`, `0x7B1`, `0x7B3`, `0x7B7`, `0x7A0`).
     - Continuous ring-buffer + fault/manual event flight recorder (JSON + CSV export under `starpilot_storage_root() / 'ioniq' / 'recordings'`).
     - 3x-press passive CAN button learning & debounced action dispatcher.
     - Fuel consumption (`EMS20.FCO` + Kappa 1.6 GDi torque/RPM model + `CLU13` cross-check), Ignition-to-Ignition trips, and Tank-to-Tank ("Volltanken zu Volltanken") pump calibration.
   - `openpilot/system/manager/process_config.py`: `PythonProcess("ioniqd", "openpilot.starpilot.ioniq.runtime", always_run, enabled=not PC)`.
   - `openpilot/selfdrive/car/card.py`: Calls `maybe_run_startup_diagnostic(...)` during Hyundai startup OBD window before `FirmwareQueryDone` is published.
   - `openpilot/starpilot/galaxy/ioniq_tools.py`, `openpilot/starpilot/galaxy/server.py`, `openpilot/starpilot/galaxy/http_server.py`: `/api/ioniq-tools` and `/api/ioniq-tools/action` endpoints & `ioniq_tools_source` cleanup.
   - `openpilot/starpilot/galaxy/web/js/ioniq-tools.js` & `openpilot/starpilot/galaxy/web/js/vehicle-controls.js`: `<IoniqToolsPage>` UI inside Vehicle Controls.
   - **Prebuilt `Dom` Storage Invariant**: All custom Ioniq persistent state lives in JSON under `starpilot_storage_root() / 'ioniq'`, NEVER adding new keys to `openpilot/common/params_keys.h` unless `libparams_c.so` is rebuilt too.

3. **Merged Upstream Feature Branches (Also kept intact as separate PR branches)**
   - `vienna-speed-limit`: Vienna convention speed-limit sign styling & UI toggle (`PR branch` on `StarPilotHQ/StarPilot`).
   - `custom-cloud-provider`: Custom connect/cloud endpoint support alongside comma/konik (`PR branch` on `StarPilotHQ/StarPilot`).

## Merge Conflict Policy

- Preserve all files in `openpilot/starpilot/ioniq/`, `openpilot/starpilot/galaxy/ioniq_tools.py`, `openpilot/starpilot/galaxy/web/js/ioniq-tools.js`, `openpilot/starpilot/galaxy/tests/test_ioniq_tools.mjs`, `.github/workflows/upstream-sync.yml`, and `FORK_DIVERGENCES.md`.
- In `server.py`, `http_server.py`, `vehicle-controls.js`, `process_config.py`, and `card.py`, accept upstream improvements while keeping the small `ioniq_*` / `IoniqToolsPage` hooks intact.
- Respect Galaxy static preview contract (`openpilot/starpilot/galaxy/tests/test_preview_contract.py`): keep `web/data/catalog.json` tool paths and literal `fetch("...")` allowlist untouched.
