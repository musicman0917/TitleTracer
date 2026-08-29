#!/usr/bin/env python3
"""Thin entry point so the tool can be run as `python titletracer.py ...`
in addition to `python -m titletracer`."""

from titletracer.cli import main

if __name__ == "__main__":
    main()
