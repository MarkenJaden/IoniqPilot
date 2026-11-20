"""Convert Mapillary (MTSD v2) and GTSRB/GTSDB datasets into detector + 64x64 classifier splits."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from openpilot.starpilot.speed_limits.vision.eu_training.classes import (
  DETECTOR_GROUPS,
  EU_SIGN_CLASSES,
  resolve_gtsrb_class,
  resolve_mtsd_label,
)


def yolo_box_line(detector_group: int, xmin: float, ymin: float, xmax: float, ymax: float,
                  img_w: int, img_h: int) -> str | None:
  if img_w <= 0 or img_h <= 0 or xmax <= xmin or ymax <= ymin:
    return None
  cx = max(0.0, min(1.0, ((xmin + xmax) * 0.5) / img_w))
  cy = max(0.0, min(1.0, ((ymin + ymax) * 0.5) / img_h))
  bw = max(0.001, min(1.0, (xmax - xmin) / img_w))
  bh = max(0.001, min(1.0, (ymax - ymin) / img_h))
  return f'{detector_group} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}'


def convert_mtsd_annotations(mtsd_annotations_dir: Path, out_labels_dir: Path) -> int:
  out_labels_dir.mkdir(parents=True, exist_ok=True)
  converted = 0
  for ann_file in sorted(mtsd_annotations_dir.glob('*.json')):
    data = json.loads(ann_file.read_text(encoding='utf-8'))
    width = int(data.get('width', 0))
    height = int(data.get('height', 0))
    lines: list[str] = []
    for obj in data.get('objects', ()):
      cls = resolve_mtsd_label(str(obj.get('label', '')))
      if cls is None:
        continue
      bbox = obj.get('bbox', {})
      line = yolo_box_line(
        cls.detector_group,
        float(bbox.get('xmin', 0.0)),
        float(bbox.get('ymin', 0.0)),
        float(bbox.get('xmax', 0.0)),
        float(bbox.get('ymax', 0.0)),
        width,
        height,
      )
      if line is not None:
        lines.append(line)
    if lines:
      (out_labels_dir / f'{ann_file.stem}.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
      converted += 1
  return converted


def parse_gtsrb_csv(csv_path: Path) -> list[dict[str, object]]:
  rows: list[dict[str, object]] = []
  with csv_path.open('r', encoding='utf-8') as handle:
    reader = csv.DictReader(handle, delimiter=';')
    for row in reader:
      cls = resolve_gtsrb_class(int(row['ClassId']))
      if cls is None:
        continue
      rows.append({
        'filename': row['Filename'],
        'class_id': cls.class_id,
        'class_name': cls.name,
        'roi': (int(row['Roi.X1']), int(row['Roi.Y1']), int(row['Roi.X2']), int(row['Roi.Y2'])),
      })
  return rows


def write_dataset_yaml(out_dir: Path) -> Path:
  out_dir.mkdir(parents=True, exist_ok=True)
  yaml_path = out_dir / 'eu_signs_detector.yaml'
  names_block = '\n'.join(f'  {gid}: {gname}' for gid, gname in sorted(DETECTOR_GROUPS.items()))
  content = (
    f'path: {out_dir.as_posix()}\n'
    'train: images/train\n'
    'val: images/val\n'
    'names:\n'
    f'{names_block}\n'
  )
  yaml_path.write_text(content, encoding='utf-8')
  meta_path = out_dir / 'eu_classifier_classes.json'
  meta_path.write_text(
    json.dumps([c.__dict__ for c in EU_SIGN_CLASSES], indent=2) + '\n',
    encoding='utf-8',
  )
  return yaml_path


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--mtsd-annotations', type=Path, help='Path to MTSD v2 annotations/ directory')
  parser.add_argument('--gtsrb-csv', type=Path, help='Path to GTSRB GT-final_test.csv or Train.csv')
  parser.add_argument('--out-dir', type=Path, required=True, help='Output directory for prepared dataset')
  args = parser.parse_args()

  yaml_path = write_dataset_yaml(args.out_dir)
  print(f'Wrote detector config: {yaml_path}')
  if args.mtsd_annotations and args.mtsd_annotations.is_dir():
    count = convert_mtsd_annotations(args.mtsd_annotations, args.out_dir / 'labels' / 'train')
    print(f'Converted {count} MTSD images with German/EU speed signs.')
  if args.gtsrb_csv and args.gtsrb_csv.is_file():
    samples = parse_gtsrb_csv(args.gtsrb_csv)
    (args.out_dir / 'gtsrb_manifest.json').write_text(json.dumps(samples, indent=2) + '\n', encoding='utf-8')
    print(f'Indexed {len(samples)} GTSRB crop samples.')


if __name__ == '__main__':
  main()
