"""Bounded source staging for trusted local operators; not an untrusted sandbox."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import uuid4
import zipfile

MAX_ZIP = 20 * 1024 * 1024
MAX_EXPANDED = 50 * 1024 * 1024
MAX_FILES = 2000
MAX_MEMBERS = 4000
FORBIDDEN = {".git", ".aws", ".ssh", ".kube", ".local", ".venv", ".venv-ui", "node_modules"}
RESERVED = {"con", "prn", "aux", "nul", *("com" + str(i) for i in range(1, 10)), *("lpt" + str(i) for i in range(1, 10))}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)


def safe_member(name):
    if not name or "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        raise ValueError("Archive paths must be relative POSIX paths")
    parts = PurePosixPath(name).parts
    if not parts or len(parts) > 20:
        raise ValueError("Archive path is empty or too deeply nested")
    for part in parts:
        if part in {"..", "."} or part.endswith((" ", ".")) or any(ord(c) < 32 for c in part):
            raise ValueError("Unsafe archive path")
        if part.casefold() in FORBIDDEN or part.split(".")[0].casefold() in RESERVED:
            raise ValueError("Archive contains a forbidden directory or device name")
        if part.casefold() in {".env", "docker.sock"} or part.casefold().endswith((".pem", ".key", ".kubeconfig")):
            raise ValueError("Remove credentials, private keys and local environment files from the project")
    return parts


def extract_zip(data, destination):
    if len(data) > MAX_ZIP:
        raise ValueError("ZIP exceeds the 20 MiB compressed limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("Upload is not a valid ZIP archive") from exc
    with archive:
        members = archive.infolist()
        if not members or len(members) > MAX_MEMBERS:
            raise ValueError("Archive is empty or exceeds 4,000 entries")
        seen, total, count, validated = set(), 0, 0, []
        for member in members:
            parts = safe_member(member.filename)
            key = "/".join(parts).casefold()
            if key in seen:
                raise ValueError("Duplicate/case-colliding archive paths are forbidden")
            seen.add(key)
            kind = stat.S_IFMT(member.external_attr >> 16)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or member.flag_bits & 1:
                raise ValueError("Symlinks, special files and encrypted archives are forbidden")
            if member.is_dir():
                continue
            count += 1
            total += member.file_size
            if count > MAX_FILES or total > MAX_EXPANDED or member.file_size > MAX_ZIP:
                raise ValueError("ZIP exceeds 2,000 files, 20 MiB/file or 50 MiB expanded")
            if member.file_size > max(1, member.compress_size) * 100:
                raise ValueError("Suspicious ZIP compression ratio exceeds 100:1")
            validated.append((member, parts))
        if not validated:
            raise ValueError("ZIP contains no application files")
        # Accept the usual single project-folder wrapper without guessing filenames.
        top = {parts[0] for _, parts in validated}
        strip = len(top) == 1 and all(len(parts) > 1 for _, parts in validated)
        actual = 0
        destination.mkdir(parents=True, exist_ok=False)
        for member, parts in validated:
            target = destination.joinpath(*(parts[1:] if strip else parts))
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Archive extraction escaped its staging directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("xb") as output:
                written = 0
                while chunk := source.read(64 * 1024):
                    actual += len(chunk)
                    written += len(chunk)
                    if actual > MAX_EXPANDED or written > member.file_size:
                        raise ValueError("Expanded ZIP size exceeds its declared limits")
                    output.write(chunk)
        return {"files": count, "expanded_bytes": actual, "archive_sha256": hashlib.sha256(data).hexdigest()}


class GithubRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {"api.github.com", "codeload.github.com"} or parsed.username or parsed.port:
            raise ValueError("GitHub import redirected outside the permitted HTTPS hosts")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download(url, limit):
    opener = build_opener(GithubRedirects())
    deadline = time.monotonic() + 90
    result = bytearray()
    request = Request(url, headers={"User-Agent": "manufact-local-mcp-demo", "Accept": "application/vnd.github+json"})
    with opener.open(request, timeout=20) as response:
        while chunk := response.read(64 * 1024):
            result.extend(chunk)
            if len(result) > limit or time.monotonic() > deadline:
                raise ValueError("GitHub download exceeded its size/time limit")
    return bytes(result)


class Sources:
    def __init__(self, root):
        self.root = root
        self.folder = root / ".local/ui/imports"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.registry_path = root / ".local/ui/sources.json"
        self.registered = json.loads(self.registry_path.read_text()) if self.registry_path.exists() else {}
        self.trusted = json.loads((root / "ui/trusted_repositories.json").read_text())["repositories"]

    def stage(self, kind, *, data=None, url=None, template=None):
        if len(list(self.folder.iterdir())) >= 20:
            raise ValueError("Local staging is full (20 imports). An operator must archive/remove old staging directories.")
        identifier = uuid4().hex
        stage = self.folder / identifier
        stage.mkdir()
        try:
            source = {"kind": kind}
            if kind == "github":
                if url not in self.trusted or not re.fullmatch(r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
                    raise ValueError("Repository URL is not explicitly trusted in ui/trusted_repositories.json")
                repository = url.removeprefix("https://github.com/")
                commit = json.loads(download("https://api.github.com/repos/" + repository + "/commits/HEAD", 1024 * 1024))["sha"]
                if not re.fullmatch(r"[0-9a-f]{40}", commit):
                    raise ValueError("GitHub did not return a resolved commit SHA")
                data = download("https://codeload.github.com/" + repository + "/zip/" + commit, MAX_ZIP)
                source.update(url=url, commit=commit)
            elif kind == "template":
                if template not in {"stateless", "stateful"}:
                    raise ValueError("Choose an approved example template")
                buffer = io.BytesIO()
                folder = self.root / "servers" / template
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in folder.rglob("*"):
                        if "__pycache__" in path.parts:
                            continue
                        if path.is_symlink() or path.is_junction():
                            raise ValueError("Template contains an unsafe link")
                        if path.is_file():
                            archive.write(path, path.relative_to(folder).as_posix())
                data = buffer.getvalue()
                source["template"] = template
            elif kind != "zip":
                raise ValueError("Choose ZIP, GitHub or an example template")
            stats = extract_zip(data or b"", stage / "project")
            config = stage / "project/mcp.json"
            if config.exists():
                if config.stat().st_size > 64 * 1024:
                    raise ValueError("mcp.json exceeds 64 KiB")
                initial = json.loads(config.read_text(encoding="utf-8"))
                if not isinstance(initial, dict):
                    raise ValueError("mcp.json must contain a JSON object")
                if not isinstance(initial.get("build", {}), dict) or not isinstance(initial.get("env", {}), dict):
                    raise ValueError("mcp.json build and env fields must be objects")
            else:
                initial = {}
            metadata = {"id": identifier, "created_at": datetime.now(timezone.utc).isoformat(),
                        "source": {**source, **stats}, "has_config": config.exists(), "initial": initial}
            atomic_json(stage / "stage.json", metadata)
            return metadata
        except Exception:
            # Verified fresh UUID path inside this module's staging root only.
            if stage.resolve().parent != self.folder.resolve():
                raise RuntimeError("Refusing staging cleanup outside import root")
            shutil.rmtree(stage)
            raise

    def get(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Invalid import identifier")
        path = self.folder / identifier / "stage.json"
        if not path.is_file():
            raise ValueError("Import not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def register(self, identifier, project):
        if identifier in self.registered:
            raise ValueError("This source has already been registered")
        self.registered[identifier] = project
        atomic_json(self.registry_path, self.registered)
