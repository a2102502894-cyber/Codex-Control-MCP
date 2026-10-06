from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
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

    def _sync_runtime(self, skill, record, commit=None):
        """Stage first, preserve the previous runtime until state commits."""
        target = self.runtime_root / skill
        active = str(record.get("active") or "").strip()
        temp = self.runtime_root / ("." + skill + ".sync-" + uuid.uuid4().hex)
        backup = self.runtime_root / ("." + skill + ".backup-" + uuid.uuid4().hex)
        old_moved = new_moved = False
        try:
            if active:
                meta = record.get("versions", {}).get(active)
                if not meta or not (pathlib.Path(meta["path"]) / "SKILL.md").is_file():
                    raise BridgeError("skill_state_corrupt", "Active Skill package is missing.")
                self._copy_bounded(pathlib.Path(meta["path"]), temp, 64 * 1024 * 1024)
                if self._digest(temp, 64 * 1024 * 1024)[0] != meta.get("digest"):
                    raise BridgeError("skill_digest_mismatch", "Installed Skill content changed before activation.")
            if target.exists() or target.is_symlink():
                target.replace(backup)
                old_moved = True
            if active:
                temp.replace(target)
                new_moved = True
            if commit is not None:
                commit()
        except Exception:
            try:
                if new_moved:
                    target.replace(temp)
                if old_moved:
                    backup.replace(target)
            except Exception as exc:
                raise BridgeError("skill_switch_unverified", "Skill switch and restoration could not be confirmed; inspect the preserved recovery paths before retrying.",
                                  details={"runtime_path": str(target), "backup_path": str(backup), "automatic_retry_performed": False}) from exc
            raise
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        # A cleanup failure is observable; it never removes the restored old
        # runtime or falsely reports physical deletion of leftover packages.
        cleanup_pending = []
        if old_moved:
            try:
                if backup.is_symlink(): backup.unlink()
                else: shutil.rmtree(backup)
            except OSError:
                cleanup_pending.append(str(backup))
        return {"runtime_path": str(target), "runtime_active": active,
                "synced": True, "cleanup_pending": cleanup_pending}

    def _commit_record(self, skill, record, *, sync=True, remove=False):
        previous = self.state
        candidate = copy.deepcopy(previous)
        if remove:
            candidate.setdefault("skills", {}).pop(skill, None)
        else:
            candidate.setdefault("skills", {})[skill] = record
        def commit():
            self.state = candidate
            try:
                self._save()
            except Exception:
                self.state = previous
                raise
        if sync:
            return self._sync_runtime(skill, record, commit)
        commit()
        return {"synced": False, "runtime_active": record.get("active", "")}

    def _skill_name(self, value):
        value = str(value or "").strip()
        if not NAME_RE.fullmatch(value):
            raise BridgeError("skill_invalid", "Skill name is missing or invalid.")
        return value

    def _load_state(self):
        if not self.state_path.exists():
            return {"skills": {}}
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("skill_state_corrupt", "Skill state cannot be read.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
            raise BridgeError("skill_state_corrupt", "Skill state must contain a skills object.")
        return data

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

    def _inventory(self, root, max_bytes, include_directories=False):
        files, folders, total, entries = [], [], 0, 0
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            raise BridgeError("skill_invalid", "Skill roots cannot be links.")
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__"})
            for name in dirs + sorted(names):
                entries += 1
                if entries > 4096:
                    raise BridgeError("skill_too_large", "Skill package exceeds its entry budget.")
                path = pathlib.Path(directory) / name
                st = path.lstat()
                if path.is_symlink() or getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                    raise BridgeError("skill_invalid", "Skill packages cannot contain links or reparse points.")
                if name in dirs:
                    folders.append(path.relative_to(root))
                    continue
                if not stat.S_ISREG(st.st_mode):
                    raise BridgeError("skill_invalid", "Skill packages must contain regular files only.")
                total += st.st_size
                if total > max_bytes or len(files) >= 4096:
                    raise BridgeError("skill_too_large", "Skill package exceeds its byte or file budget.")
                files.append(path.relative_to(root))
        return (sorted(files), sorted(folders)) if include_directories else sorted(files)

    def _copy_bounded(self, source, destination, max_bytes):
        files, folders = self._inventory(source, max_bytes, include_directories=True)
        total = 0
        destination.mkdir(parents=True, exist_ok=False)
        for rel in folders:
            (destination / rel).mkdir(parents=True, exist_ok=True)
        for rel in files:
            src, dst = source / rel, destination / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Recheck each path immediately before opening, including parents.
            for part in [src, *src.parents]:
                if part == source.parent: break
                if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
                    raise BridgeError("skill_invalid", "Skill source changed into a link.")
            with src.open("rb") as reader, dst.open("xb") as writer:
                while True:
                    chunk = reader.read(min(65536, max_bytes - total + 1))
                    if not chunk: break
                    total += len(chunk)
                    if total > max_bytes:
                        raise BridgeError("skill_too_large", "Skill source grew beyond max_bytes.")
                    writer.write(chunk)
            shutil.copystat(src, dst, follow_symlinks=False)

    def _digest(self, root, max_bytes):
        total, h, files = 0, hashlib.sha256(), []
        for rel in self._inventory(root, max_bytes):
            h.update(rel.as_posix().encode("utf-8") + b"\0")
            with (root / rel).open("rb") as reader:
                while True:
                    chunk = reader.read(min(65536, max_bytes - total + 1))
                    if not chunk: break
                    total += len(chunk)
                    if total > max_bytes:
                        raise BridgeError("skill_too_large", "Skill package exceeds max_bytes.")
                    h.update(chunk)
            h.update(b"\0")
            files.append(rel.as_posix())
        return h.hexdigest(), total, files

    def _extract_bounded(self, archive, destination, max_bytes):
        members, seen, folders, declared = [], set(), set(), 0
        for member in archive.infolist():
            name = member.filename
            parts = pathlib.PurePosixPath(name).parts
            if not parts or "\\" in name or ":" in name or name.startswith("/") or ".." in parts or stat.S_ISLNK(member.external_attr >> 16):
                raise BridgeError("skill_invalid", "Zip contains an unsafe path or link.")
            if len(seen) >= 4096:
                raise BridgeError("skill_too_large", "Archive exceeds its entry budget.")
            if any(p in {".git", "__pycache__"} for p in parts): continue
            key = "/".join(parts).casefold()
            if key in seen:
                raise BridgeError("skill_invalid", "Zip contains duplicate paths.")
            seen.add(key)
            for index in range(1, len(parts) + (1 if member.is_dir() else 0)):
                folders.add(parts[:index])
                if len(folders) + len(members) > 4096:
                    raise BridgeError("skill_too_large", "Archive exceeds its directory budget.")
            if member.is_dir(): continue
            declared += member.file_size
            if declared > max_bytes or len(members) >= 4096:
                raise BridgeError("skill_too_large", "Archive exceeds its byte or file budget.")
            members.append(member)
        destination.mkdir(parents=True, exist_ok=False)
        for parts in sorted(folders):
            destination.joinpath(*parts).mkdir(parents=True, exist_ok=True)
        total = 0
        for member in members:
            target = destination.joinpath(*pathlib.PurePosixPath(member.filename).parts)
            target.resolve().relative_to(destination.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as reader, target.open("xb") as writer:
                while True:
                    chunk = reader.read(min(65536, max_bytes - total + 1))
                    if not chunk: break
                    total += len(chunk)
                    if total > max_bytes:
                        raise BridgeError("skill_too_large", "Archive exceeds max_bytes.")
                    writer.write(chunk)

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
                    self._copy_bounded(self._package_root(path), temp / "source", max_bytes)
                    root = self._package_root(temp / "source")
                elif path.is_file() and path.suffix.lower() == ".zip":
                    with zipfile.ZipFile(path) as archive:
                        self._extract_bounded(archive, temp / "source", max_bytes)
                    root = self._package_root(temp / "source")
                else:
                    raise BridgeError("skill_source_error", "Skill source must be a directory, zip, or Git URL.")
            fallback_name = root.name
            if root != temp / "source":
                self._copy_bounded(root, temp / "source", max_bytes)
                root = temp / "source"
            body = (root / "SKILL.md").read_text("utf-8")
            name, description, version = self._frontmatter(body, fallback_name)
            digest, total, files = self._digest(root, max_bytes)
            return temp, root, {"name": name, "description": description, "version": version, "digest": digest, "bytes": total, "files": files, "body": body}
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    def _record(self, skill):
        skill = self._skill_name(skill)
        return copy.deepcopy(self.state.get("skills", {}).get(skill, {"active": "", "history": [], "versions": {}}))

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
        new_destination = None
        try:
            expected = str(args.get("digest") or "").strip().lower()
            if expected and expected != info["digest"]:
                raise BridgeError("skill_digest_mismatch", "Skill package digest does not match expected SHA-256.")
            version = str(args.get("version") or info["version"] or ("sha256-" + info["digest"][:12])).strip()
            if not version or version in {".", ".."} or version.endswith(".") or any(ch in version for ch in "\\/:*?\"<>|"):
                raise BridgeError("skill_invalid", "Skill version is invalid.")
            destination = self.root / info["name"] / version
            record = self._record(info["name"])
            existing = record["versions"].get(version)
            if destination.exists():
                if not existing or existing.get("digest") != info["digest"]:
                    raise BridgeError("skill_version_exists", "A different package already occupies this Skill version.")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                stage = destination.parent / (".install-" + uuid.uuid4().hex)
                try:
                    self._copy_bounded(root, stage, max_bytes)
                    stage.replace(destination)
                    new_destination = destination
                finally:
                    shutil.rmtree(stage, ignore_errors=True)
            record["versions"][version] = {"version": version, "digest": info["digest"], "channel": channel, "description": info["description"], "path": str(destination), "installed_at": utc_now()}
            activate = bool(args.get("activate", True))
            previous = record.get("active", "")
            if activate:
                if previous and previous != version:
                    record.setdefault("history", []).append(previous)
                    record["history"] = record["history"][-20:]
                record["active"] = version
            sync = self._commit_record(info["name"], record, sync=activate)
            new_destination = None
            return {"action": "install", "skill": info["name"], "version": version, "digest": info["digest"], "channel": channel, "path": str(destination), "activated": activate, "previous_active": previous, **sync}
        finally:
            if new_destination is not None:
                shutil.rmtree(new_destination)
            shutil.rmtree(temp, ignore_errors=True)

    def activate(self, args):
        skill = str(args.get("skill") or "").strip(); version = str(args.get("version") or "").strip()
        record = self._record(skill)
        if version not in record.get("versions", {}):
            raise BridgeError("skill_not_found", "Requested Skill version is not installed.")
        previous = record.get("active", "")
        if previous and previous != version:
            record.setdefault("history", []).append(previous); record["history"] = record["history"][-20:]
        record["active"] = version
        sync = self._commit_record(skill, record)
        return {"action": "activate", "skill": skill, "from_version": previous, "to_version": version, **sync}

    def rollback(self, args):
        skill = str(args.get("skill") or "").strip(); record = self._record(skill)
        history = record.get("history") or []
        while history:
            version = history.pop()
            if version in record.get("versions", {}):
                previous = record.get("active", ""); record["active"] = version; record["history"] = history
                sync = self._commit_record(skill, record)
                return {"action": "rollback", "skill": skill, "from_version": previous, "to_version": version, "verified": True, **sync}
        raise BridgeError("skill_rollback_unavailable", "No installed previous Skill version is available.")

    def uninstall(self, args):
        skill = self._skill_name(args.get("skill")); version = str(args.get("version") or "").strip()
        original = self.state.get("skills", {}).get(skill)
        if not original:
            raise BridgeError("skill_not_found", "Skill is not installed.")
        record = copy.deepcopy(original)
        targets = [version] if version else list(record.get("versions", {}))
        if version and version not in record.get("versions", {}):
            raise BridgeError("skill_not_found", "Skill version is not installed.")
        moved = []
        try:
            for item in targets:
                meta = record["versions"].pop(item)
                path = pathlib.Path(meta["path"])
                if path.exists():
                    trash = path.parent / (".remove-" + uuid.uuid4().hex)
                    path.replace(trash)
                    moved.append((path, trash))
            if record.get("active") in targets: record["active"] = ""
            record["history"] = [x for x in record.get("history", []) if x not in targets]
            sync = self._commit_record(skill, record, remove=not record["versions"])
        except Exception:
            try:
                for path, trash in reversed(moved): trash.replace(path)
            except Exception as exc:
                raise BridgeError("skill_switch_unverified", "Skill uninstall rollback could not be confirmed; inspect preserved packages before retrying.",
                                  details={"recovery_paths": [str(trash) for _, trash in moved if trash.exists()], "automatic_retry_performed": False}) from exc
            raise
        pending = []
        for _, trash in moved:
            try: shutil.rmtree(trash)
            except OSError: pending.append(str(trash))
        return {"action": "uninstall", "skill": skill, "removed_versions": targets,
                "active_version": record.get("active", ""), **sync,
                "cleanup_pending": sync.get("cleanup_pending", []) + pending}

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
