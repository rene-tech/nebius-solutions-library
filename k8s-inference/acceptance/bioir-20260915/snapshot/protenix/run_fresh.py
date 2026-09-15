#!/usr/bin/env python3
"""Explicit independent authorized cohort; preserve all prior artifacts."""
import subprocess
import sys
import control
import operate

assert control.COHORT in {"xjaw", "y0jt"}, "Explicit replacement cohort required"
assert not (control.ROOT / "manifests").exists(), "Never overwrite an existing cohort"
control.prepare()
name = control.PREFIX + "-donor"
control.donor(name, "protenix-" + control.COHORT + "-globalrng-r1")
operate.ready(name)
operate.request(name, "ubiquitin-76", "warm-prepared")
operate.capture(name)
operate.release(name)
subprocess.run([sys.executable, str(control.BASE / "matrix.py")], check=True)
