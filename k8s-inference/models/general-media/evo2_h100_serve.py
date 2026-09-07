#!/usr/bin/env python3
"""Compose the retained HTTP server with the explicit two-H100 backend."""
import evo2_serve
from evo2_h100 import H100Backend

evo2_serve.Evo2Backend = H100Backend

if __name__ == "__main__":
    raise SystemExit(evo2_serve.main())
