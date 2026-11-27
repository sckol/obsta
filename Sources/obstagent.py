#!/usr/bin/env python3
"""Compatibility entry point for single-vault invocations."""
import sys
from pathlib import Path

if __name__ == '__main__':
    sources = Path(__file__).resolve().parent
    sys.path.insert(0, str(sources / 'obstagent'))
    from obsta.worker import main

    sys.exit(main(['--vault', str(sources.parent), *sys.argv[1:]]))
