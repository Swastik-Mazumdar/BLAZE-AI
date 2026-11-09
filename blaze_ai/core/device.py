"""Device capability probe used to recommend and label local models."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class DeviceProfile:
    system: str
    release: str
    machine: str
    chip: str
    total_ram: int
    accelerator: str
    max_params_b: float

    @property
    def ram_gb(self) -> float:
        return self.total_ram / (1024**3)

    @property
    def summary(self) -> str:
        parts = [self.system, self.machine]
        if self.chip and self.chip.lower() != self.machine.lower():
            parts.append(self.chip)
        parts.append(f"{self.ram_gb:.0f} GB RAM")
        parts.append(self.accelerator)
        return " · ".join(part for part in parts if part)

    @property
    def target_label(self) -> str:
        return f"TARGET ≤ {self.max_params_b:.0f}B PARAMS"


def _chip() -> str:
    if sys.platform == "darwin":
        for key in ("machdep.cpu.brand_string", "hw.model"):
            try:
                value = subprocess.run(
                    ["sysctl", "-n", key],
                    capture_output=True,
                    text=True,
                    timeout=2,
                ).stdout.strip()
            except Exception:
                value = ""
            if value:
                return value
    if os.name == "nt":
        return platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
    return platform.processor() or platform.machine()


def _accelerator(machine: str) -> str:
    if sys.platform == "darwin" and machine.lower() in {"arm64", "aarch64"}:
        return "Apple Silicon · Metal"
    if shutil.which("nvidia-smi"):
        return "NVIDIA GPU"
    if sys.platform == "darwin":
        return "CPU"
    if os.name == "nt":
        return "CPU / DirectML"
    return "CPU"


def device_profile() -> DeviceProfile:
    machine = platform.machine() or "unknown"
    total_ram = int(psutil.virtual_memory().total)
    ram_gb = total_ram / (1024**3)
    # Leave headroom for the OS, Qt and the KV cache. This is deliberately
    # conservative so a recommendation actually fits in unified/system RAM.
    max_params = max(0.5, round((ram_gb - 3.0) * 0.45, 1))
    return DeviceProfile(
        system=platform.system() or sys.platform,
        release=platform.release(),
        machine=machine,
        chip=_chip(),
        total_ram=total_ram,
        accelerator=_accelerator(machine),
        max_params_b=max_params,
    )
