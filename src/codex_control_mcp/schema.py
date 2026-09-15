from __future__ import annotations
import json, os, pathlib, re, tempfile, uuid
from jsonschema import Draft7Validator
from .common import atomic_json, digest, run_metadata
from .errors import BridgeError

RESPONSE_NAMES = {
    "initialize": "InitializeResponse",
    "command/exec": "CommandExecResponse",
    "command/exec/write": "CommandExecWriteResponse",
    "command/exec/terminate": "CommandExecTerminateResponse",
    "command/exec/resize": "CommandExecResizeResponse",
    "fs/readFile": "FsReadFileResponse",
    "fs/writeFile": "FsWriteFileResponse",
    "fs/getMetadata": "FsGetMetadataResponse",
    "fs/readDirectory": "FsReadDirectoryResponse",
    "fs/remove": "FsRemoveResponse",
    "fs/createDirectory": "FsCreateDirectoryResponse",
    "mcpServerStatus/list": "ListMcpServerStatusResponse",
    "remoteControl/status/read": "RemoteControlStatusReadResponse",
}
ALLOWED_METHODS = frozenset(RESPONSE_NAMES)


class SchemaRegistry:
    def __init__(self, folder):
        self.folder = folder
        self.doc = json.loads((folder / "ClientRequest.json").read_text("utf-8"))
        self.methods = {}
        for variant in self.doc.get("oneOf", self.doc.get("anyOf", [])):
            p = variant.get("properties", {})
            m = p.get("method", {}).get("const", p.get("method", {}).get("enum", []))
            if isinstance(m, list):
                m = m[0] if m else None
            if m:
                self.methods[m] = p.get("params", {})
        self._validators = {}
        self.responses = {}
        self.response_documents = {}
        for method, name in RESPONSE_NAMES.items():
            matches = list(folder.glob("**/" + name + ".json"))
            if matches:
                self.response_documents[method] = json.loads(
                    matches[0].read_text("utf-8")
                )
                self.responses[method] = Draft7Validator(
                    self.response_documents[method]
                )
        self.hash = digest({"requests": self.doc, "responses": self.response_documents})

    def validate(self, method, params):
        if method not in ALLOWED_METHODS:
            raise BridgeError(
                "capability_unavailable",
                "RPC is not in the reviewed model-free execution allowlist.",
            )
        if method not in self.methods:
            raise BridgeError(
                "capability_unavailable", f"Installed Codex does not expose {method}."
            )
        if method not in self._validators:
            self._validators[method] = Draft7Validator(
                {
                    **self.methods[method],
                    "definitions": self.doc.get("definitions", {}),
                    "$schema": "http://json-schema.org/draft-07/schema#",
                }
            )
        error = next(self._validators[method].iter_errors(params), None)
        if error:
            raise BridgeError(
                "version_incompatible",
                f"{method} failed Schema at {list(error.absolute_path)} (rule={error.validator}).",
            )

    def validate_response(self, method, result):
        v = self.responses.get(method)
        if v:
            error = next(v.iter_errors(result), None)
            if error:
                raise BridgeError(
                    "version_incompatible",
                    f"{method} returned an incompatible response (rule={error.validator}).",
                )

    def params_properties(self, method):
        s = self.methods.get(method, {})
        if "$ref" in s:
            s = self.doc.get("definitions", {}).get(s["$ref"].split("/")[-1], {})
        return s.get("properties", {})

    def method_contract(self, method):
        def expand(obj, seen=()):
            if isinstance(obj, dict):
                if "$ref" in obj:
                    ref = obj["$ref"].split("/")[-1]
                    if ref in seen:
                        return {"$recursive_ref": ref}
                    return expand(
                        self.doc.get("definitions", {}).get(ref, obj), seen + (ref,)
                    )
                return {
                    k: expand(v, seen)
                    for k, v in obj.items()
                    if k not in ("description", "title", "$schema", "default")
                }
            if isinstance(obj, list):
                return [expand(v, seen) for v in obj]
            return obj

        return {
            "request": expand(self.methods.get(method)),
            "response": self.response_documents.get(method),
        }

    def compare(self, old):
        current = set(self.methods)
        previous = set(old.methods)
        changed = [
            m
            for m in sorted(current & previous)
            if self.method_contract(m) != old.method_contract(m)
        ]
        removed = sorted((previous - current) & ALLOWED_METHODS)
        return {
            "added": sorted(current - previous),
            "removed": sorted(previous - current),
            "changed_contracts": changed,
            "used_methods_removed": removed,
            "known_adapters_retest_required": sorted(set(changed) & ALLOWED_METHODS),
            "structural_status": "incompatible"
            if removed
            else ("retest_required" if set(changed) & ALLOWED_METHODS else "unchanged"),
            "behavior_verified": False,
        }


def load_or_export(cfg, runtime, force=False):
    cache = cfg.home / "cache/schemas"

    # The official export contains long filenames; Windows MAX_PATH also
    # affects Python stat/glob/delete unless the cache uses extended paths.
    def cache_path(value):
        value = str(pathlib.Path(value).resolve())
        if os.name == "nt" and not value.startswith("\\\\?\\"):
            value = (
                "\\\\?\\UNC\\" + value[2:]
                if value.startswith("\\\\")
                else "\\\\?\\" + value
            )
        return pathlib.Path(value)

    cache = cache_path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    root = cache / runtime.file_fingerprint
    label = "experimental" if cfg.experimental else "stable"
    manifest = root / "hash.json"
    cacheinfo = {}
    if manifest.is_file():
        try:
            cacheinfo = json.loads(manifest.read_text("utf-8"))
            if not isinstance(cacheinfo, dict):
                cacheinfo = {}
        except (ValueError, OSError):
            pass
    generation = cacheinfo.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(
        r"g-[a-f0-9]{16}", generation
    ):
        generation = None
    selected = (root / generation if generation else root) / label
    state = cfg.home / "state/schema.json"
    oldinfo = {}
    if state.exists():
        try:
            oldinfo = json.loads(state.read_text("utf-8"))
        except ValueError:
            pass
    oldreg = None
    oldpath = oldinfo.get("schema_path")
    if oldpath and cache_path(oldpath).is_dir():
        try:
            oldreg = SchemaRegistry(cache_path(oldpath))
        except (OSError, ValueError):
            pass
    exported = False
    if not force and (selected / "ClientRequest.json").is_file():
        try:
            reg = SchemaRegistry(selected)
            force = not manifest.exists() or cacheinfo.get("selected_hash") != reg.hash
        except (ValueError, OSError):
            force = True
    if force or not (selected / "ClientRequest.json").is_file():
        help_ = run_metadata(
            [runtime.codex_path, "app-server", "generate-json-schema", "--help"]
        )
        if help_.returncode or "--out" not in help_.stdout:
            raise BridgeError(
                "version_incompatible", "Codex cannot export the required JSON Schema."
            )
        if cfg.experimental and "--experimental" not in help_.stdout:
            raise BridgeError(
                "version_incompatible", "Experimental schema unsupported."
            )
        with tempfile.TemporaryDirectory(prefix="schema-", dir=cache) as temp:
            tmp = pathlib.Path(temp)
            for export_label in (
                ("stable", "experimental") if cfg.experimental else ("stable",)
            ):
                args = [
                    runtime.codex_path,
                    "app-server",
                    "generate-json-schema",
                    "--out",
                    str(tmp / export_label),
                ]
                if export_label == "experimental":
                    args.append("--experimental")
                r = run_metadata(args, timeout=60)
                if r.returncode:
                    raise BridgeError(
                        "version_incompatible",
                        "Official schema export failed.",
                        details={"exit_code": r.returncode},
                    )
                SchemaRegistry(tmp / export_label)
            root.mkdir(parents=True, exist_ok=True)
            # Publish a complete immutable generation, then replace its pointer.
            # Never delete the last usable schemas before a refresh succeeds.
            generation = "g-" + uuid.uuid4().hex[:16]
            tmp.rename(root / generation)
            selected = root / generation / label
        reg = SchemaRegistry(selected)
        exported = True
        atomic_json(
            root / "hash.json", {"selected_hash": reg.hash, "generation": generation}
        )
    else:
        reg = SchemaRegistry(selected)
    difference = (
        reg.compare(oldreg)
        if oldreg
        else {"structural_status": "first_scan", "behavior_verified": False}
    )
    record = {
        "fingerprint": runtime.file_fingerprint,
        "schema_hash": reg.hash,
        "schema_path": str(selected),
        "exported": exported,
        "difference": difference,
    }
    atomic_json(state, record)
    return reg, record
