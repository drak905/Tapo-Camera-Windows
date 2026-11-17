"""PyInstaller entry point for TapoDesktop.exe."""
import sys

from tapo_desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
