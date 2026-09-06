"""
Entry point for running erza as a module: python -m erza
"""

import sys

from erza.cli.commands import app

if __name__ == "__main__":
    sys.exit(app())
