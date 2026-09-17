#!/usr/bin/env python3
"""Three fresh restores, three cold-process controls, and identity fallback."""
import argparse
import control
import operate

parser = argparse.ArgumentParser()
parser.add_argument("--donor", default=control.PREFIX + "-donor")
parser.add_argument("--first-restore-done", action="store_true")
parser.add_argument("--restore-start", type=int)
args = parser.parse_args()
for index in range(args.restore_start or (2 if args.first_restore_done else 1), 4):
    name = f"{control.PREFIX}-restore-{index}"
    operate.restore(args.donor, name)
    operate.ready(name)
    operate.request(name, "ubiquitin-76,lysozyme-129", "measured")
    operate.release(name)
for index in range(1, 4):
    name = f"{control.PREFIX}-normal-{index}"
    control.donor(name, f"protenix-normal-{index}")
    operate.ready(name)
    # Match the captured donor's complete request history. Readiness clocks
    # end before warm76; a normal control then executes76→129→76, versus
    # donor76→capture→fresh restore→129→76. First-valid shape differs and
    # must not be treated as a matched startup-to-first-output speed ratio.
    operate.request(name, "ubiquitin-76", "first")
    operate.request(name, "ubiquitin-76,lysozyme-129", "measured")
    operate.release(name)
name = control.PREFIX + "-fallback"
operate.restore(args.donor, name, fallback=True)
operate.ready(name)
operate.request(name, "ubiquitin-76,lysozyme-129", "measured")
operate.release(name)
control.preflight("matrix-complete")
