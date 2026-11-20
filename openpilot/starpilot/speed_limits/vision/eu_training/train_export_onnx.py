"""Train and export 2-stage German/EU traffic sign ONNX models (256x256 detector + 64x64 classifier).

Requires PyTorch, torchvision, onnx, and ultralytics on the training workstation/GPU.
Never imported by onroad processes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from openpilot.starpilot.speed_limits.vision.eu_training.classes import EU_SIGN_CLASSES

DETECTOR_ONNX_NAME = 'traffic_signs_eu_detector.onnx'
CLASSIFIER_ONNX_NAME = 'speed_limit_eu_value_classifier.onnx'
DETECTOR_SIZE = 256
CLASSIFIER_SIZE = 64
ONNX_OPSET = 17


def sha256_file(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as handle:
    for chunk in iter(lambda: handle.read(65536), b''):
      digest.update(chunk)
  return digest.hexdigest()


def export_classifier_onnx(weights_path: Path | None, out_onnx: Path) -> str:
  import torch
  import torchvision.models as models

  model = models.mobilenet_v3_small(weights=None, num_classes=len(EU_SIGN_CLASSES))
  if weights_path is not None and weights_path.is_file():
    state = torch.load(weights_path, map_location='cpu', weights_only=True)
    model.load_state_dict(state)
  model.eval()
  dummy = torch.zeros(1, 3, CLASSIFIER_SIZE, CLASSIFIER_SIZE, dtype=torch.float32)
  out_onnx.parent.mkdir(parents=True, exist_ok=True)
  torch.onnx.export(
    model,
    dummy,
    str(out_onnx),
    opset_version=ONNX_OPSET,
    input_names=['input'],
    output_names=['logits'],
    dynamic_axes=None,
  )
  return sha256_file(out_onnx)


def write_provenance_manifest(out_dir: Path, detector_sha: str | None, classifier_sha: str | None) -> Path:
  manifest = {
    'schema': 'eu_vision_sign_bundle_v1',
    'detector_file': DETECTOR_ONNX_NAME,
    'detector_sha256': detector_sha,
    'detector_input_shape': [1, 3, DETECTOR_SIZE, DETECTOR_SIZE],
    'classifier_file': CLASSIFIER_ONNX_NAME,
    'classifier_sha256': classifier_sha,
    'classifier_input_shape': [1, 3, CLASSIFIER_SIZE, CLASSIFIER_SIZE],
    'num_classes': len(EU_SIGN_CLASSES),
    'classes': [c.__dict__ for c in EU_SIGN_CLASSES],
  }
  path = out_dir / 'eu_vision_manifest.json'
  path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
  return path


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--classifier-weights', type=Path, help='Trained PyTorch state_dict for 64x64 classifier')
  parser.add_argument('--export-untrained-scaffold', action='store_true',
                      help='Export an untrained 64x64 ONNX classifier to verify tensor ABI')
  parser.add_argument('--out-dir', type=Path, required=True, help='Output directory for ONNX assets and manifest')
  args = parser.parse_args()

  args.out_dir.mkdir(parents=True, exist_ok=True)
  classifier_sha = None
  if args.classifier_weights or args.export_untrained_scaffold:
    classifier_onnx = args.out_dir / CLASSIFIER_ONNX_NAME
    classifier_sha = export_classifier_onnx(args.classifier_weights, classifier_onnx)
    print(f'Exported {classifier_onnx} (sha256={classifier_sha})')
  manifest_path = write_provenance_manifest(args.out_dir, None, classifier_sha)
  print(f'Wrote EU vision manifest: {manifest_path}')


if __name__ == '__main__':
  main()
