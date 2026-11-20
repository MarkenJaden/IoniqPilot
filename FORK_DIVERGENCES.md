# Fork Divergences — IoniqPilot (`Dom` vs `StarPilotHQ/StarPilot` `Dom`)

> Maintained for `MarkenJaden/workflows` automated upstream sync (`@main`).
> Custom 2016 Hyundai Ioniq Hybrid implementations in this repository MUST be preserved across upstream merges and are NOT pushed upstream to `StarPilotHQ/StarPilot`.

## Preserved Fork Features

1. **Upstream Sync Master Workflow**
   - `.github/workflows/upstream-sync.yml` references `MarkenJaden/workflows/.github/workflows/upstream-sync.yml@main` with `workflow_sync_policy: preserve_if_no_pat`.
   - `FORK_DIVERGENCES.md` documents fork-owned paths and invariants.

2. **2016 Hyundai Ioniq Hybrid (`HYUNDAI_IONIQ` / AE HEV) Custom Suite & Stock-SCC Auto-Main**
   - `openpilot/starpilot/ioniq/` (`can_signals.py`, `can_discovery.py`, `dtc_catalog.py`, `uds_dtc.py`, `recorder.py`, `button_learner.py`, `fuel_tracker.py`, `runtime.py`, `storage.py`, `tests/`):
     - Always-on passive CAN fault monitoring & live C-CAN ECU matrix (`ECM`, `TCU`, `HCU`, `SCC`, `ESC`, `MDPS`, `MFC`, `CLU`, `DATC`, `IBS`, `EPB`, `TPMS`, `ACU`, `BSD`; with 8s boot/bulb-check blanking, SNA sentinel filtering, and multi-sample debounce) + time-bounded (`<=2.2s`) dual-bus (`OBD-II` Bus 1 + `C-CAN` Bus 0) `ELM327` ISO-TP UDS/OBD-II ECU DTC scan, Mode 01 supported-PID/readiness & Mode 02 freeze-frame, UDS `0x19 0x04` snapshots, and clear (`0x7E0..0x7E4`, `0x7D0..0x7D4`, `0x7C4`, `0x7C6`, `0x7B1`, `0x7B3`, `0x7B7`, `0x7A0`).
     - 12V IBS auxiliary battery & LDC charging monitor (`BAT11`) and HEV 1.6 GDi thermal warm-up monitor (`EMS12`/`EMS14`/`EMS15`/`EMS19`/`DATC`).
     - Continuous ring-buffer + fault/manual event flight recorder (`recordings/`) and 1 Hz raw CAN fuel-discovery logger & counter ranker (`discovery/`, JSON export).
     - 3x-press passive CAN button learning & debounced action dispatcher.
     - Multi-source fuel consumption (`EMS20 0x200` FCO + guarded `EMS14 0x545` FCO + OBD-II live PID + Kappa 1.6 GDi torque/RPM model + `CLU13` cluster average fallback), Ignition-to-Ignition trips, stationary-guarded automatic `Volltanken` detection, and Tank-to-Tank pump/cost calibration.
   - `opendbc_repo/opendbc/car/hyundai/carcontroller.py`, `opendbc_repo/opendbc/car/hyundai/hyundaican.py`, `opendbc_repo/opendbc/car/hyundai/tests/test_classic_auto_main.py`, `opendbc_repo/opendbc/safety/modes/hyundai.h`, `opendbc_repo/opendbc/safety/tests/test_hyundai.py`:
     - Automatic Cruise `Main` standby activation (`CLU11` `CF_Clu_CruiseSwMain=1`) after ignition on classic stock-SCC Hyundai (`HYUNDAI_IONIQ`), inhibited on manual `Cancel` or `Main` off.
   - `openpilot/selfdrive/selfdrived/selfdrived.py`, `openpilot/selfdrive/car/card.py`:
     - Time-bounded (`<=2.2s`) `card.py` startup diagnostic hook (`maybe_run_startup_diagnostic`, active only when `scanOnStart` or a queued diagnostic action is set) paired with `selfdrived.py` 10s startup grace timers anchored to `self.initialized_frame` so cold-boot `Main -> SET` never triggers false `Controls Mismatch`.
   - `openpilot/system/manager/process_config.py`: `PythonProcess("ioniqd", "openpilot.starpilot.ioniq.runtime", always_run, enabled=not PC, restart_on_exit=True)` (ordered after core Openpilot processes).
   - `openpilot/system/manager/process_health.py`: Lists `'ioniqd'` in `ANCILLARY_PROCESSES` so `ioniqd` startup/restarts never block Openpilot engagement or trigger `processNotRunning`.
   - `openpilot/starpilot/galaxy/ioniq_tools.py`, `openpilot/starpilot/galaxy/server.py`, `openpilot/starpilot/galaxy/http_server.py`: `/api/ioniq-tools` and `/api/ioniq-tools/action` endpoints & `ioniq_tools_source` cleanup.
   - `openpilot/starpilot/galaxy/web/js/ioniq-tools.js`, `openpilot/starpilot/galaxy/web/data/catalog.json`, `openpilot/starpilot/galaxy/web/js/app.js`: Standalone `<IoniqToolsPage>` under Galaxy **Tools** (`/ioniq`).
   - **Prebuilt `Dom` Storage Invariant**: All custom Ioniq persistent state lives in JSON under `starpilot_storage_root() / 'ioniq'`, NEVER adding new keys to `openpilot/common/params_keys.h` unless `libparams_c.so` is rebuilt too.

3. **Merged Upstream Feature Branches (Also kept intact as separate PR branches)**
   - `vienna-speed-limit`: Vienna convention speed-limit sign styling & UI toggle (`PR branch` on `StarPilotHQ/StarPilot`).
   - `custom-cloud-provider`: Custom connect/cloud endpoint support alongside comma/konik (`PR branch` on `StarPilotHQ/StarPilot`).

## Merge Conflict Policy

- Preserve all files in `openpilot/starpilot/ioniq/`, `openpilot/starpilot/galaxy/ioniq_tools.py`, `openpilot/starpilot/galaxy/web/js/ioniq-tools.js`, `openpilot/starpilot/galaxy/tests/test_ioniq_tools.mjs`, `opendbc_repo/opendbc/car/hyundai/tests/test_classic_auto_main.py`, `.github/workflows/upstream-sync.yml`, and `FORK_DIVERGENCES.md`.
- In `server.py`, `http_server.py`, `app.js`, `catalog.json`, `process_health.py`, `process_config.py`, `card.py`, `selfdrived.py`, `carcontroller.py`, `hyundaican.py`, and `hyundai.h`, accept upstream improvements while keeping the `ioniq_*` / `IoniqToolsPage` / `ioniqd` / `auto_cruise_main` hooks intact.
- Respect Galaxy static preview contract (`openpilot/starpilot/galaxy/tests/test_preview_contract.py`): keep `web/data/catalog.json` `/ioniq` entry and literal `fetch("...")` allowlist aligned.
