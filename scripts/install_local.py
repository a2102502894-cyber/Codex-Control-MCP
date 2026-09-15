"""Compatibility entry point; the historical hard-coded 0.1.0 installer is retired.

Usage: python scripts/install_local.py --release <verified release directory>
Installation is delegated to the current versioned deployer and its checks.
"""

from deploy_release import main

if __name__ == "__main__":
    raise SystemExit(main())
