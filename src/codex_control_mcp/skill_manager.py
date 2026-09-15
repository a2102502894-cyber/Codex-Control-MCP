from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import uuid
import zipfile

from .common import atomic_json, utc_now
from .errors import BridgeError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHANNELS = {"stable", "development", "canary", "pinned"}


class SkillManager:
    """Versioned SKILL.md package store with activation and rollback."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.root = pathlib.Path(cfg.home) / "skills"
        self.root.mkdir(parents=True, exist_ok=True)
        runtime_home = pathlib.Path(
            getattr(cfg, "runtime_home", pathlib.Path(cfg.home) / "runtime-home")
        )
        self.runtime_root = runtime_home / "skills"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.state_path = pathlib.Path(cfg.home) / "state" / "skills.json"
        self.state = self._load_state()

    def _sync_runtime(self, skill, record):
        """Materialize the active package into CODEX_HOME/skills for real Codex discovery."""
        target = self.runtime_root / skill
        active = str(record.get("active") or "").strip()
        if not active:
            shutil.rmtree(target, ignore_errors=True)
            return {"runtime_path": str(target), "runtime_active": "", "synced": True}
        meta = record.get("versions", {}).get(active)
        if not meta:
            raise BridgeError("skill_state_corrupt", "Active Skill version metadata is missing.")
        source = pathlib.Path(meta["path"])
        if not (source / "SKILL.md").is_file():
            raise BridgeError("skill_state_corrupt", "Active Skill package is missing SKILL.md.")
        temp = self.runtime_root / ("." + skill + ".sync-" + uuid.uuid4().hex)
        shutil.copytree(source, temp)
        try:
            shutil.rmtree(target, ignore_errors=True)
            temp.replace(target)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        return {
            "runtime_path": str(target),
            "runtime_active": active,
            "synced": True,
        }

    def _load_state(self):
        if not self.state_path.exists():
            return {"skills": {}}
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("skill_state_corrupt", "Skill state cannot be read.") from exc
        return data if isinstance(data, dict) else {"skills": {}}

    def _save(self):
        atomic_json(self.state_path, self.state)

    def _git(self):
        choices = [
            getattr(self.cfg, "git_path", None),
            str(pathlib.Path(self.cfg.home) / "tools" / "mingit" / "cmd" / "git.exe"),
            shutil.which("git"),
        ]
        for choice in choices:
            if choice and pathlib.Path(choice).is_file():
                return str(choice)
        raise BridgeError("skill_git_missing", "Git is required for remote Skill sources.")

    def _frontmatter(self, body, fallback_name):
        meta = {}
        lines = body.splitlines()
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip().lower()] = value.strip().strip('"').strip("'")
        name = meta.get("name") or fallback_name
        if not NAME_RE.fullmatch(name):
            raise BridgeError("skill_invalid", "Skill name is missing or invalid.")
        description = meta.get("description", "")
        version = meta.get("version", "")
        return name, description, version

    def _package_root(self, path):
        path = pathlib.Path(path)
        if (path / "SKILL.md").is_file():
            return path
        matches = [p.parent for p in path.glob("*/SKILL.md")]
        if len(matches) == 1:
            return matches[0]
        raise BridgeError("skill_invalid", "Source must contain exactly one SKILL.md package root.")

    def _digest(self, root, max_bytes):
        total = 0
        h = hashlib.sha256()
        files = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            total += len(raw)
            if total > max_bytes:
                raise BridgeError("skill_too_large", "Skill package exceeds max_bytes.")
            h.update(rel.encode("utf-8") + b"\0" + raw + b"\0")
            files.append(rel)
        return h.hexdigest(), total, files

    def _stage(self, source, max_bytes):
        source = str(source or "").strip()
        if not source:
            raise BridgeError("invalid_arguments", "source is required.")
        temp = pathlib.Path(tempfile.mkdtemp(prefix="ccm-skill-"))
        try:
            parsed = urllib.parse.urlparse(source)
            if parsed.scheme in {"http", "https"}:
                result = subprocess.run(
                    [self._git(), "clone", "--depth", "1", source, str(temp / "repo")],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode:
                    raise BridgeError("skill_source_error", "Git Skill source could not be cloned.")
                root = self._package_root(temp / "repo")
            else:
                path = pathlib.Path(source).expanduser().resolve()
                if path.is_dir():
                    shutil.copytree(path, temp / "source", dirs_exist_ok=True)
                    root = self._package_root(temp / "source")
                elif path.is_file() and path.suffix.lower() == ".zip":
                    with zipfile.ZipFile(path) as archive:
                        for member in archive.infolist():
                            target = (temp / "source" / member.filename).resolve()
                            if not str(target).startswith(str((temp / "source").resolve())):
                                raise BridgeError("skill_invalid", "Zip contains path traversal.")
                        archive.extractall(temp / "source")
                    root = self._package_root(temp / "source")
                else:
                    raise BridgeError("skill_source_error", "Skill source must be a directory, zip, or Git URL.")
            body = (root / "SKILL.md").read_text("utf-8")
            name, description, version = self._frontmatter(body, root.name)
            digest, total, files = self._digest(root, max_bytes)
            return temp, root, {"name": name, "description": description, "version": version, "digest": digest, "bytes": total, "files": files, "body": body}
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    def _record(self, skill):
        return self.state.setdefault("skills", {}).setdefault(skill, {"active": "", "history": [], "versions": {}})

    def validate(self, args):
        max_bytes = max(1024, min(int(args.get("max_bytes") or 16 * 1024 * 1024), 64 * 1024 * 1024))
        temp, root, info = self._stage(args.get("source"), max_bytes)
        try:
            expected = str(args.get("digest") or "").strip().lower()
            if expected and expected != info["digest"]:
                raise BridgeError("skill_digest_mismatch", "Skill package digest does not match expected SHA-256.")
            return {"action": "validate", "valid": True, "skill": info["name"], "version": info["version"] or "auto", "digest": info["digest"], "bytes": info["bytes"], "files": info["files"], "description": info["description"]}
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def install(self, args):
        max_bytes = max(1024, min(int(args.get("max_bytes") or 16 * 1024 * 1024), 64 * 1024 * 1024))
        channel = str(args.get("channel") or "stable").strip().lower()
        if channel not in CHANNELS:
            raise BridgeError("invalid_arguments", "channel must be stable, development, canary, or pinned.")
        temp, root, info = self._stage(args.get("source"), max_bytes)
        try:
            expected = str(args.get("digest") or "").strip().lower()
            if expected and expected != info["digest"]:
                raise BridgeError("skill_digest_mismatch", "Skill package digest does not match expected SHA-256.")
            version = str(args.get("version") or info["version"] or ("sha256-" + info["digest"][:12])).strip()
            if not version or any(ch in version for ch in "\\/:*?\"<>|"):
                raise BridgeError("skill_invalid", "Skill version is invalid.")
            destination = self.root / info["name"] / version
            record = self._record(info["name"])
            existing = record["versions"].get(version)
            if destination.exists():
                if not existing or existing.get("digest") != info["digest"]:
                    raise BridgeError("skill_version_exists", "A different package already occupies this Skill version.")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(root, destination)
            record["versions"][version] = {"version": version, "digest": info["digest"], "channel": channel, "description": info["description"], "path": str(destination), "installed_at": utc_now()}
            activate = bool(args.get("activate", True))
            previous = record.get("active", "")
            if activate:
                if previous and previous != version:
                    record.setdefault("history", []).append(previous)
                    record["history"] = record["history"][-20:]
                record["active"] = version
            self._save()
            sync = self._sync_runtime(info["name"], record) if activate else {"synced": False, "runtime_active": record.get("active", "")}
            return {"action": "install", "skill": info["name"], "version": version, "digest": info["digest"], "channel": channel, "path": str(destination), "activated": activate, "previous_active": previous, **sync}
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def activate(self, args):
        skill = str(args.get("skill") or "").strip(); version = str(args.get("version") or "").strip()
        record = self._record(skill)
        if version not in record.get("versions", {}):
            raise BridgeError("skill_not_found", "Requested Skill version is not installed.")
        previous = record.get("active", "")
        if previous and previous != version:
            record.setdefault("history", []).append(previous); record["history"] = record["history"][-20:]
        record["active"] = version; self._save()
        sync = self._sync_runtime(skill, record)
        return {"action": "activate", "skill": skill, "from_version": previous, "to_version": version, **sync}

    def rollback(self, args):
        skill = str(args.get("skill") or "").strip(); record = self._record(skill)
        history = record.get("history") or []
        while history:
            version = history.pop()
            if version in record.get("versions", {}):
                previous = record.get("active", ""); record["active"] = version; record["history"] = history; self._save()
                sync = self._sync_runtime(skill, record)
                return {"action": "rollback", "skill": skill, "from_version": previous, "to_version": version, "verified": True, **sync}
        raise BridgeError("skill_rollback_unavailable", "No installed previous Skill version is available.")

    def uninstall(self, args):
        skill = str(args.get("skill") or "").strip(); version = str(args.get("version") or "").strip()
        record = self.state.get("skills", {}).get(skill)
        if not record:
            raise BridgeError("skill_not_found", "Skill is not installed.")
        targets = [version] if version else list(record.get("versions", {}))
        if version and version not in record.get("versions", {}):
            raise BridgeError("skill_not_found", "Skill version is not installed.")
        removed = []
        for item in targets:
            meta = record["versions"].pop(item, None)
            if meta:
                shutil.rmtree(meta["path"], ignore_errors=True); removed.append(item)
        if record.get("active") in removed:
            record["active"] = ""
        record["history"] = [x for x in record.get("history", []) if x not in removed]
        if not record["versions"]:
            self.state["skills"].pop(skill, None); shutil.rmtree(self.root / skill, ignore_errors=True)
        self._save()
        sync = self._sync_runtime(skill, record)
        return {"action": "uninstall", "skill": skill, "removed_versions": removed, "active_version": record.get("active", ""), **sync}

    def list(self):
        items = []
        for name, record in sorted(self.state.get("skills", {}).items()):
            items.append({"skill": name, "active_version": record.get("active", ""), "versions": sorted(record.get("versions", {})), "version_count": len(record.get("versions", {}))})
        return {"action": "list", "skills": items, "count": len(items), "root": str(self.root)}

    def inspect(self, args):
        skill = str(args.get("skill") or "").strip(); record = self.state.get("skills", {}).get(skill)
        if not record:
            raise BridgeError("skill_not_found", "Skill is not installed.")
        version = str(args.get("version") or record.get("active") or "").strip()
        meta = record.get("versions", {}).get(version)
        if not meta:
            raise BridgeError("skill_not_found", "Skill version is not installed or active.")
        body = (pathlib.Path(meta["path"]) / "SKILL.md").read_text("utf-8")
        return {"action": "inspect", "skill": skill, "version": version, "active": version == record.get("active"), "metadata": meta, "body": body}

    def package(self, args):
        action = str(args.get("action") or "").strip().lower()
        if action == "validate": return self.validate(args)
        if action == "install": return self.install(args)
        if action == "activate": return self.activate(args)
        if action == "rollback": return self.rollback(args)
        if action == "uninstall": return self.uninstall(args)
        if action == "list": return self.list()
        if action == "inspect": return self.inspect(args)
        raise BridgeError("invalid_arguments", "Unsupported skill_package action.", details={"allowed": ["validate", "install", "activate", "rollback", "uninstall", "list", "inspect"]})
