"""Backward-compatible entry point for the HappyOyster international runner.

``bench_runner_happyoyster.py`` now uses the international site by default.
This filename remains available so existing commands continue to work.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import bench_runner_happyoyster as runner


def main() -> None:
    runner.main()


if __name__ == "__main__":
    main()
