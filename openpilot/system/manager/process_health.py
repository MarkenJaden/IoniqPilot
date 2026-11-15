"""Driving health requirements for manager-owned services."""

# Projection, local phone access, and optional Ioniq helper telemetry are non-driving services.
# Their lifecycles remain visible in managerState and their own status.
# A dead Jetlink owner sends the drive back to the small model.
ANCILLARY_PROCESSES = frozenset({'starportd', 'galaxy_hotspot', 'jetlinkd', 'ioniqd'})


def driving_process_failures(processes) -> set[str]:
  return {process.name for process in processes
          if process.shouldBeRunning and not process.running and process.name not in ANCILLARY_PROCESSES}
