#!/usr/bin/env python3
from pathlib import Path

from registry_tool.cli import main


if __name__ == "__main__":
    main(project_root=Path(__file__).resolve().parent.parent)
