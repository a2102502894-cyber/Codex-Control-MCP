"""Compatibility entry point for the versioned, rollback-capable deployer.

Usage: python scripts/install_release.py --release <verified release directory>
No legacy version, executable checksum, or installed path is assumed.
"""

from deploy_release import main

if __name__ == "__main__":
    raise SystemExit(main())
