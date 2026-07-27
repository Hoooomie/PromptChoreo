"""StreamAVBench runner for the HappyOyster international site.

This keeps the domestic ``happyoyster.cn`` runner unchanged while using:

- ``https://www.happyoyster.com/create/directing``;
- a separate persistent browser profile;
- a separate ``outputs/happyoyster_global`` result tree.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import bench_runner_happyoyster as runner
from src.promptchoreo.adapters.happy_oyster_global import (
    HappyOysterGlobalAdapter,
)


def main() -> None:
    runner.ADAPTER_CLASS = HappyOysterGlobalAdapter
    runner.MODEL_ID = "happyoyster_global"
    runner.MODEL_NAME = "HappyOyster Global"
    runner.main()


if __name__ == "__main__":
    main()
