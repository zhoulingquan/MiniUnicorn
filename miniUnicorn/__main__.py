"""
Entry point for running miniUnicorn as a module: python -m miniUnicorn
"""

import sys

from miniUnicorn.cli.commands import app

if __name__ == "__main__":
    sys.exit(app())
