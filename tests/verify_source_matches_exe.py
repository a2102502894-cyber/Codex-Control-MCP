"""Compare every bundled bridge Python code object to freshly compiled source.

This checks the released executable itself, independently of a source manifest
created after a build. Nothing from the executable is executed by this verifier.
"""

import argparse
import hashlib
import json
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader

root = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("executable", type=Path)
ap.add_argument("--output", type=Path, default=root / "evidence/source-runtime-verification.json")
args = ap.parse_args()
container = CArchiveReader(str(args.executable))
archive = container.open_embedded_archive("PYZ.pyz")
modules = {}
for name in archive.toc:
    if name == "codex_control_mcp" or name.startswith("codex_control_mcp."):
        path = root / "src" / Path(*name.split("."))
        path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        actual = archive.extract(name)
        expected = compile(
            path.read_bytes(), actual.co_filename, "exec", dont_inherit=True, optimize=0
        )
        modules[name] = {
            "equal_code_object": actual == expected,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
worker_name = next((name for name in container.toc if name.replace('\\', '/') == 'codex_control_mcp/tabbit_worker.cjs'), None)
worker = root / 'src/codex_control_mcp/tabbit_worker.cjs'
worker_equal = bool(worker_name) and container.extract(worker_name) == worker.read_bytes()
report = {
    "executable": str(args.executable.resolve()),
    "sha256": hashlib.sha256(args.executable.read_bytes()).hexdigest(),
    "comparison": "Python code-object equality, including bytecode, constants, nested functions and line tables; no execution",
    "modules": modules,
    "tabbit_worker": {'equal_bytes': worker_equal, 'source_sha256': hashlib.sha256(worker.read_bytes()).hexdigest()},
    "pass": bool(modules) and all(v["equal_code_object"] for v in modules.values()) and worker_equal,
}
args.output.write_text(
    json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
)
print(
    json.dumps(
        {
            "pass": report["pass"],
            "modules": len(modules),
            "mismatches": [k for k, v in modules.items() if not v["equal_code_object"]],
        },
        ensure_ascii=True,
    )
)
raise SystemExit(0 if report["pass"] else 1)
