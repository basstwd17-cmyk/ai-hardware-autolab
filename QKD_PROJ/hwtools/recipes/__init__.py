"""Measurement recipes -- the variable half of layer 3.

A recipe decides what to measure and how much data is enough: channels, scale,
timebase, trigger, measurement type, sample count, retrieval path. It is
written for one experiment and is expected to be replaced rather than
generalised. Read `.claude/skills/scope-measure/SKILL.md` before writing one.

A recipe must not:
  - judge PASS/FAIL from anything but gate.pass_criteria()
  - enable an output without a gate check
  - omit sample_count and retrieval_path from its measurements
"""

from .gate1_gpio_toggle import gate1_gpio_toggle
from .profile_channel_capture import profile_channel_capture
from .probe_comp_check import probe_comp_check

__all__ = ["gate1_gpio_toggle", "profile_channel_capture", "probe_comp_check"]
