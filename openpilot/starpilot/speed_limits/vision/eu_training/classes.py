"""Canonical German / EU traffic sign class taxonomy and dataset label mappings."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EuSignClass:
  class_id: int
  name: str
  detector_group: int  # 0: speed_limit_274, 1: end_limit_278_282, 2: city_sign_310_311
  speed_kph: int | None  # None for unlimited (Zeichen 278 / 282)
  unlimited: bool = False


EU_SIGN_CLASSES: tuple[EuSignClass, ...] = (
  EuSignClass(0, 'limit_5', 0, 5),
  EuSignClass(1, 'limit_10', 0, 10),
  EuSignClass(2, 'limit_20', 0, 20),
  EuSignClass(3, 'limit_30', 0, 30),
  EuSignClass(4, 'limit_40', 0, 40),
  EuSignClass(5, 'limit_50', 0, 50),
  EuSignClass(6, 'limit_60', 0, 60),
  EuSignClass(7, 'limit_70', 0, 70),
  EuSignClass(8, 'limit_80', 0, 80),
  EuSignClass(9, 'limit_90', 0, 90),
  EuSignClass(10, 'limit_100', 0, 100),
  EuSignClass(11, 'limit_110', 0, 110),
  EuSignClass(12, 'limit_120', 0, 120),
  EuSignClass(13, 'limit_130', 0, 130),
  EuSignClass(14, 'unlimited_282', 1, None, unlimited=True),
  EuSignClass(15, 'end_limit_278', 1, None, unlimited=True),
  EuSignClass(16, 'city_entry_310', 2, 50),
  EuSignClass(17, 'city_exit_311', 2, 100),
)

CLASS_BY_ID: dict[int, EuSignClass] = {c.class_id: c for c in EU_SIGN_CLASSES}
CLASS_BY_NAME: dict[str, EuSignClass] = {c.name: c for c in EU_SIGN_CLASSES}

DETECTOR_GROUPS: dict[int, str] = {
  0: 'speed_limit_274',
  1: 'end_limit_278_282',
  2: 'city_sign_310_311',
}

# German Traffic Sign Recognition Benchmark (GTSRB) class ID -> EuSignClass name
GTSRB_TO_EU_CLASS: dict[int, str] = {
  0: 'limit_20',
  1: 'limit_30',
  2: 'limit_50',
  3: 'limit_60',
  4: 'limit_70',
  5: 'limit_80',
  6: 'end_limit_278',     # Ende der Geschwindigkeitsbegrenzung (80)
  7: 'limit_100',
  8: 'limit_120',
  32: 'unlimited_282',    # Ende aller Streckenverbote (Zeichen 282)
}

# Mapillary Traffic Sign Dataset (MTSD v2) label -> EuSignClass name
MTSD_TO_EU_CLASS: dict[str, str] = {
  'regulatory--maximum-speed-limit-5--g1': 'limit_5',
  'regulatory--maximum-speed-limit-10--g1': 'limit_10',
  'regulatory--maximum-speed-limit-20--g1': 'limit_20',
  'regulatory--maximum-speed-limit-30--g1': 'limit_30',
  'regulatory--maximum-speed-limit-40--g1': 'limit_40',
  'regulatory--maximum-speed-limit-50--g1': 'limit_50',
  'regulatory--maximum-speed-limit-60--g1': 'limit_60',
  'regulatory--maximum-speed-limit-70--g1': 'limit_70',
  'regulatory--maximum-speed-limit-80--g1': 'limit_80',
  'regulatory--maximum-speed-limit-90--g1': 'limit_90',
  'regulatory--maximum-speed-limit-100--g1': 'limit_100',
  'regulatory--maximum-speed-limit-110--g1': 'limit_110',
  'regulatory--maximum-speed-limit-120--g1': 'limit_120',
  'regulatory--maximum-speed-limit-130--g1': 'limit_130',
  'regulatory--end-of-maximum-speed-limit-30--g1': 'end_limit_278',
  'regulatory--end-of-maximum-speed-limit-50--g1': 'end_limit_278',
  'regulatory--end-of-maximum-speed-limit-60--g1': 'end_limit_278',
  'regulatory--end-of-maximum-speed-limit-70--g1': 'end_limit_278',
  'regulatory--end-of-maximum-speed-limit-80--g1': 'end_limit_278',
  'regulatory--end-of-maximum-speed-limit-100--g1': 'end_limit_278',
  'regulatory--end-of-maximum-speed-limit-120--g1': 'end_limit_278',
  'regulatory--end-of-speed-limit-zone--g1': 'end_limit_278',
  'regulatory--end-of-prohibition--g1': 'unlimited_282',
}


def resolve_mtsd_label(label: str) -> EuSignClass | None:
  name = MTSD_TO_EU_CLASS.get(label.strip())
  return CLASS_BY_NAME.get(name) if name is not None else None


def resolve_gtsrb_class(gtsrb_id: int) -> EuSignClass | None:
  name = GTSRB_TO_EU_CLASS.get(int(gtsrb_id))
  return CLASS_BY_NAME.get(name) if name is not None else None
