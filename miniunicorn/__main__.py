"""
Entry point for running miniunicorn as a module: python -m miniunicorn
"""

import sys

from miniunicorn.cli.commands import app

if __name__ == "__main__":
    sys.exit(app())
