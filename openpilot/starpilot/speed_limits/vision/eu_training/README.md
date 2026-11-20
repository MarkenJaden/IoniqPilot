# German / EU Traffic Sign Vision Pipeline (Phase 2 Blueprint & Training Scaffold)

## 1. Why the Current US Vision Pipeline Fails on German Roads

StarPilot's active vision speed-limit pipeline (`openpilot/starpilot/speed_limits/vision/`) uses:
- `speed_limit_us_detector.onnx` / `traffic_signs_us_detector.onnx` (`256×256` RGB input)
- `traffic_sign_identity.onnx` (`64×64` RGB crop verifier)
- `speed_limit_us_value_classifier.onnx` (`64×64` RGB crop classifier)

Three hard constraints prevent it from working in Germany/Europe:
1. **Red-ring HSV pre-filter**: `vision/model.py` defines `REGULATORY_MAX_RED_RATIO = 0.10`, which actively **rejects** any sign with more than 10% red pixels (designed to filter out US STOP/Interstate shields, but rejects all German Vienna-convention Zeichen 274 red-ring speed limits!).
2. **US-only detector geometry**: Trained exclusively on rectangular white MUTCD `SPEED LIMIT` plates (`aspect_ratio` filters and rectangular contour checks).
3. **Imperial-only value classes**: `US_CLASSIFIER_SPEED_VALUES = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 90, 100)` lacks **110, 120, 130 km/h**, lacks **Zeichen 278 / 282 (Unbegrenzt / Aufhebung)**, and converts every output assuming **mph** (`speed_limit_mph * 0.44704`).

---

## 2. Target German / EU Architecture (Drop-In ONNX Replacement)

To stay 100% compatible with comma 3 / 3X / 4 CPU/ONNX budgets (~15–25 ms per frame in `vision/producer.py`), we use the same 2-stage cascade:

| Stage | Model file | Input shape | Output | Architecture |
| --- | --- | --- | --- | --- |
| **1. Detector** | `traffic_signs_eu_detector.onnx` | `float32 [1, 3, 256, 256]` (`0..1` RGB) | Boxes `[1, N, 4]`, Scores `[1, C_det, N]` (`C_det = 3`: `speed_limit_274`, `end_limit_278_282`, `city_sign_310_311`) | YOLOv8n / YOLO11n (neck pruned to ~6–9 MB, opset 17) |
| **2. Classifier** | `speed_limit_eu_value_classifier.onnx` | `float32 [1, 3, 64, 64]` (`0..1` RGB letterboxed) | Logits `[1, 18]` (see `classes.py`) | MobileNetV3-Small / ResNet-18 (~3–5 MB, opset 17) |

### Canonical 18 German Sign Classes (`classes.py`)
- Numeric limits (Zeichen 274): `5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130` km/h (14 classes)
- End of speed limit / all restrictions (Zeichen 278 / Zeichen 282): `unlimited_282`, `end_limit_278` → both emit `ObservationKind.UNLIMITED`
- Built-up area boundary (Zeichen 310 / 311): `city_entry_310` (50 km/h), `city_exit_311` (100 km/h rural default)

---

## 3. Recommended Datasets

1. **Mapillary Traffic Sign Dataset (MTSD v2, Fully Annotated)**:
   - Contains high-resolution street-level bounding boxes and fine-grained Vienna-convention labels (`regulatory--maximum-speed-limit-*--g1`, `regulatory--end-of-maximum-speed-limit-*--g1`, `regulatory--end-of-speed-limit-zone--g1`).
2. **GTSDB + GTSRB (German Traffic Sign Detection & Recognition Benchmarks)**:
   - Native German road footage covering classes `0..8` (`20..120 km/h`) and classes `6, 32, 41, 42` (Zeichen 278 / 282).
3. **German Autobahn & LED Gantry Augmentation**:
   - Synthetic + real comma road-camera crops (`1928×1208` / `1344×760` right-ROI crops) for **variable LED matrix signs** (Wechselverkehrszeichen on Autobahn gantries: dark background, bright white/amber digits + red LED ring).

---

## 4. Training & Export Workflow

1. **Prepare dataset** (converts MTSD JSON + GTSRB CSVs into detector & classifier datasets):
   ```bash
   python -m openpilot.starpilot.speed_limits.vision.eu_training.prepare_dataset \
     --mtsd-root /path/to/mtsd_v2 \
     --gtsrb-root /path/to/gtsrb \
     --out-dir /path/to/eu_signs_prepared
   ```
2. **Train & export ONNX models**:
   ```bash
   python -m openpilot.starpilot.speed_limits.vision.eu_training.train_export_onnx \
     --data-dir /path/to/eu_signs_prepared \
     --out-dir openpilot/starpilot/speed_limits/vision/assets
   ```
3. **Runtime integration checklist (Phase 2)**:
   - Add `EU_REGION` mode in `vision/model.py` (selected automatically when `UseVienna` + `IsMetric` are active):
     - Relax `REGULATORY_MAX_RED_RATIO` from `0.10` to `0.55` for Vienna red-ring signs (and allow dark-interior LED gantry signs).
     - Load `traffic_signs_eu_detector.onnx` + `speed_limit_eu_value_classifier.onnx`.
     - Map `DE_CLASS_TO_KPH` directly to `speed_mps = kph / 3.6` (no mph conversion!) and `unlimited_282` / `end_limit_278` to `ObservationKind.UNLIMITED`.
