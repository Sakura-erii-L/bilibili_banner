from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from backend import capture  # noqa: E402


ADMIN_ASSETS = ROOT / "scripts" / "admin"
ADMIN_LOCK = threading.RLock()
MANAGED_PATHS = (
    "data/index.json",
    "data/admin-overrides.json",
    "data/archive",
)


def git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def preexisting_data_changes() -> str:
    try:
        output = git_output(
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *MANAGED_PATHS,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        return str(error)
    if not output:
        return ""

    allowed = {"data/index.json", "data/admin-overrides.json"}
    try:
        overrides = capture.load_admin_overrides()
        allowed_archive_prefixes = {
            f"data/archive/{_archive_folder_for_manifest(str(manifest)).name}/"
            for merge in overrides.get("merges") or []
            if isinstance(merge, dict)
            for manifest in merge.get("cleanedManifests") or []
        }
    except Exception as error:
        return str(error)
    unexpected = []
    for line in output.splitlines():
        path = line[3:].strip().strip('"')
        if path in allowed or any(path.startswith(prefix) for prefix in allowed_archive_prefixes):
            continue
        unexpected.append(line)
    return "\n".join(unexpected)


def read_index() -> dict:
    return json.loads((capture.DATA_DIR / "index.json").read_text(encoding="utf-8"))


def _status_path(line: str) -> str:
    return line[3:].strip().strip('"').replace("\\", "/")


def _merge_override_sets(base: dict, local: dict) -> dict:
    merged = dict(base)
    merged.update(local)
    by_id: dict[str, dict] = {}
    for item in base.get("merges") or []:
        if isinstance(item, dict):
            by_id[str(item.get("id") or json.dumps(item, sort_keys=True))] = item
    for item in local.get("merges") or []:
        if isinstance(item, dict):
            by_id[str(item.get("id") or json.dumps(item, sort_keys=True))] = item
    merged["version"] = 1
    merged["merges"] = list(by_id.values())
    return merged


def _restore_cleaned_manifests(overrides: dict) -> None:
    for merge in overrides.get("merges") or []:
        if not isinstance(merge, dict):
            continue
        for manifest in merge.get("cleanedManifests") or []:
            folder = _archive_folder_for_manifest(str(manifest))
            if folder.exists():
                shutil.rmtree(folder)


def _archive_folder_for_manifest(manifest: str) -> Path:
    normalized = manifest.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if len(parts) != 4 or parts[:2] != ["data", "archive"] or parts[3] != "banner.json":
        raise ValueError(f"Unexpected archive manifest path: {manifest}")
    folder = (capture.ARCHIVE_DIR / parts[2]).resolve()
    archive_root = capture.ARCHIVE_DIR.resolve()
    if folder.parent != archive_root:
        raise ValueError(f"Archive path is outside data/archive: {manifest}")
    return folder


def cleanup_archives() -> list[str]:
    overrides = capture.load_admin_overrides()
    index = read_index()
    manifests = capture.admin_cleanup_candidates(index, overrides)
    folders = {
        manifest: _archive_folder_for_manifest(manifest)
        for manifest in manifests
    }
    removed: list[str] = []
    for manifest in manifests:
        folder = folders[manifest]
        if folder.exists():
            shutil.rmtree(folder)
            removed.append(manifest)

    processed_set = set(manifests)
    if processed_set:
        for merge in overrides.get("merges") or []:
            if not isinstance(merge, dict):
                continue
            merge_cleanup = {
                str(item) for item in merge.get("cleanupManifests") or []
            }
            merge["cleanupManifests"] = sorted(merge_cleanup - processed_set)
            merge["cleanedManifests"] = sorted(
                set(str(item) for item in merge.get("cleanedManifests") or [])
                | merge_cleanup.intersection(processed_set)
            )
        capture.save_admin_overrides(overrides)
        capture.rebuild_index()
    return removed


class AdminApplication:
    def __init__(self) -> None:
        self.startup_data_changes = preexisting_data_changes()

    def state(self) -> dict:
        index = read_index()
        overrides = capture.load_admin_overrides()
        branch = ""
        git_error = ""
        ahead_commits = 0
        ahead_subjects: list[str] = []
        pending_managed_commits = 0
        try:
            branch = git_output("branch", "--show-current")
            git_output("remote", "get-url", "origin")
            ahead_commits = int(git_output("rev-list", "--count", "origin/main..HEAD"))
            ahead_subjects = [
                line for line in git_output("log", "--format=%h %s", "origin/main..HEAD").splitlines()
                if line
            ]
            pending_managed_commits = int(
                git_output("rev-list", "--count", "origin/main..HEAD", "--", "data")
            )
        except (OSError, subprocess.CalledProcessError) as error:
            git_error = str(error)
        data_root_error = ""
        if capture.DATA_DIR.resolve() != (ROOT / "data").resolve():
            data_root_error = "当前 BANNER_DATA_DIR 不在项目 data/ 内，无法通过 Pages 工作流同步。"
        try:
            managed_worktree_changes = bool(
                git_output(
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    *MANAGED_PATHS,
                )
            )
            status_lines = git_output(
                "status", "--porcelain", "--untracked-files=all"
            ).splitlines()
            non_data_changes = [
                line for line in status_lines if not _status_path(line).startswith("data/")
            ]
            mirror_sync_error = preexisting_data_changes()
            if non_data_changes:
                mirror_sync_error = "工作区有非管理数据改动；请先处理后再更新镜像。"
            elif git_output("diff", "--cached", "--name-only"):
                mirror_sync_error = "暂存区有改动；请先提交或取消暂存后再更新镜像。"
        except (OSError, subprocess.CalledProcessError) as error:
            managed_worktree_changes = False
            mirror_sync_error = str(error)
        return {
            "recordKeys": [
                capture.admin_record_key(record)
                for record in index.get("records") or []
            ],
            "recordCount": len(index.get("records") or []),
            "mirrorGeneratedAt": index.get("generatedAt", ""),
            "cleanupManifests": capture.admin_cleanup_candidates(index, overrides),
            "mergeCount": len(overrides.get("merges") or []),
            "branch": branch,
            "aheadCommits": ahead_commits,
            "aheadSubjects": ahead_subjects,
            "pendingManagedCommits": pending_managed_commits,
            "managedWorktreeChanges": managed_worktree_changes,
            "mirrorSyncError": mirror_sync_error,
            "publisherError": (
                self.startup_data_changes
                or data_root_error
                or git_error
                or mirror_sync_error
            ),
        }

    def merge(self, payload: dict) -> None:
        index = read_index()
        overrides = capture.create_admin_merge(
            index,
            [str(item) for item in payload.get("selectedKeys") or []],
            str(payload.get("primaryKey") or ""),
        )
        capture.save_admin_overrides(overrides)
        capture.rebuild_index()

    def sync_from_pages(self) -> dict:
        if capture.DATA_DIR.resolve() != (ROOT / "data").resolve():
            raise RuntimeError("当前 BANNER_DATA_DIR 不在项目 data/ 内，无法从 Pages 同步。")
        branch = git_output("branch", "--show-current")
        if branch != "main":
            raise RuntimeError(f"当前分支是 {branch or '未知'}；只能在 main 分支更新 Pages 镜像。")
        git_output("remote", "get-url", "origin")
        if git_output("diff", "--cached", "--name-only"):
            raise RuntimeError("暂存区有改动；请先提交或取消暂存后再更新镜像。")
        status_lines = git_output(
            "status", "--porcelain", "--untracked-files=all"
        ).splitlines()
        non_data_changes = [
            line for line in status_lines if not _status_path(line).startswith("data/")
        ]
        if non_data_changes:
            raise RuntimeError("工作区有非管理数据改动；请先处理后再更新镜像。")
        unexpected_data = preexisting_data_changes()
        if unexpected_data:
            raise RuntimeError(
                "检测到不属于 Banner 管理的 data/ 改动；为保护本地文件，已取消更新：\n"
                + unexpected_data
            )

        overrides_path = capture.admin_overrides_path()
        local_overrides_exist = overrides_path.exists()
        local_overrides = capture.load_admin_overrides()

        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if fetch.returncode != 0:
            details = (fetch.stderr or fetch.stdout or "git fetch 失败").strip()
            raise RuntimeError(f"无法抓取 Pages 源数据：{details}")

        # The index is derived from archives. Keep local merge rules, restore
        # only derived/source data, then regenerate against the fetched mirror.
        subprocess.run(
            ["git", "restore", "--worktree", "--source=HEAD", "--", "data/index.json", "data/archive"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if git_output("ls-files", "--", "data/admin-overrides.json"):
            subprocess.run(
                ["git", "restore", "--worktree", "--source=HEAD", "--", "data/admin-overrides.json"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        else:
            overrides_path.unlink(missing_ok=True)

        rebase = subprocess.run(
            ["git", "rebase", "origin/main"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if rebase.returncode != 0:
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            details = (rebase.stderr or rebase.stdout or "与最新 Pages 源数据同步失败").strip()
            base_overrides = capture.load_admin_overrides()
            restored_overrides = _merge_override_sets(base_overrides, local_overrides)
            if local_overrides_exist or capture.admin_overrides_path().exists():
                capture.save_admin_overrides(restored_overrides)
            _restore_cleaned_manifests(restored_overrides)
            capture.rebuild_index()
            raise RuntimeError(f"本地提交与 Pages 更新发生冲突，已还原同步前状态：{details}")

        base_overrides = capture.load_admin_overrides()
        merged_overrides = _merge_override_sets(base_overrides, local_overrides)
        if local_overrides_exist or capture.admin_overrides_path().exists():
            capture.save_admin_overrides(merged_overrides)
        _restore_cleaned_manifests(merged_overrides)
        capture.rebuild_index()
        index = read_index()
        return {
            "status": "mirrored",
            "message": (
                f"已抓取最新 Pages 源数据并更新本地镜像：索引包含 "
                f"{len(index.get('records') or [])} 条 Banner。没有推送。"
            ),
        }

    def publish(self) -> dict:
        if capture.DATA_DIR.resolve() != (ROOT / "data").resolve():
            raise RuntimeError("当前 BANNER_DATA_DIR 不在项目 data/ 内，无法通过 Pages 工作流同步。")
        if self.startup_data_changes:
            raise RuntimeError(
                "启动管理服务前，数据文件已有未提交修改。为避免夹带这些修改，请先处理 data/ 目录的现有变更后重启管理服务。"
            )
        branch = git_output("branch", "--show-current")
        if branch != "main":
            raise RuntimeError(f"当前分支是 {branch or '未知'}；只有 main 分支会触发 Pages 部署。")
        git_output("remote", "get-url", "origin")
        if git_output("diff", "--cached", "--name-only"):
            raise RuntimeError("暂存区已有其他改动；请先提交或取消暂存后再同步。")
        self.sync_from_pages()
        try:
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            ahead = int(git_output("rev-list", "--count", "origin/main..HEAD"))
            pending_managed_commits = int(
                git_output("rev-list", "--count", "origin/main..HEAD", "--", "data")
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            raise RuntimeError(
                f"本地提交尚未基于最新 origin/main；请先点击“更新本地镜像”。{error}"
            ) from error
        overrides = capture.load_admin_overrides()
        targets = ["data/index.json"]
        if capture.admin_overrides_path().exists():
            targets.append("data/admin-overrides.json")
        cleaned_folders = {
            _archive_folder_for_manifest(str(manifest)).name
            for merge in overrides.get("merges") or []
            if isinstance(merge, dict)
            for manifest in merge.get("cleanedManifests") or []
            if git_output(
                "ls-files", "--", f"data/archive/{_archive_folder_for_manifest(str(manifest)).name}"
            )
        }
        targets.extend(f"data/archive/{folder}" for folder in sorted(cleaned_folders))
        arguments = ["git", "add", "-A", "--", *targets]
        subprocess.run(arguments, cwd=ROOT, check=True)

        staged_paths = git_output("diff", "--cached", "--name-only").splitlines()
        allowed_prefixes = ("data/index.json", "data/admin-overrides.json") + tuple(
            f"data/archive/{folder}/" for folder in cleaned_folders
        )
        unexpected = [
            path
            for path in staged_paths
            if path not in allowed_prefixes[:2]
            and not any(path.startswith(prefix) for prefix in allowed_prefixes[2:])
        ]
        if unexpected:
            subprocess.run(["git", "reset", "--", *staged_paths], cwd=ROOT, check=False)
            raise RuntimeError(
                "同步范围中检测到非管理器文件，已取消暂存：" + ", ".join(unexpected)
            )
        if staged_paths:
            subprocess.run(
                ["git", "commit", "-m", "Manage archived Bilibili banners"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        elif not ahead or not pending_managed_commits:
            return {"status": "unchanged", "message": "没有需要同步的管理改动。"}

        try:
            subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except subprocess.CalledProcessError as error:
            details = (error.stderr or error.stdout or str(error)).strip()
            raise RuntimeError(
                "本地提交已创建，但推送失败；修复 Git 连接后运行 git push origin main。\n" + details
            ) from error
        return {
            "status": "pushed",
            "message": "已提交管理改动并推送到 main；GitHub Actions 将重新构建并发布 Pages。",
        }


def _safe_file(route: str) -> Path | None:
    decoded = unquote(route)
    if "\\" in decoded or ".." in Path(decoded).parts:
        return None
    if decoded.startswith("/__admin/"):
        relative = decoded.removeprefix("/__admin/")
        root = ADMIN_ASSETS
    elif decoded.startswith("/data/"):
        relative = decoded.removeprefix("/data/")
        root = capture.DATA_DIR
    elif decoded.startswith("/assets/"):
        relative = decoded.removeprefix("/assets/")
        root = ROOT / "frontend" / "assets"
    elif decoded in ("/app.js", "/interaction.js", "/style.css"):
        relative = decoded.lstrip("/")
        root = ROOT / "frontend"
    else:
        return None
    file_path = (root / relative).resolve()
    try:
        file_path.relative_to(root.resolve())
    except ValueError:
        return None
    return file_path if file_path.is_file() else None


def make_handler(application: AdminApplication, port: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BilibiliBannerLocalAdmin/1.0"

        def log_message(self, format: str, *args) -> None:
            print(f"[{self.log_date_time_string()}] {format % args}")

        def send_json(self, payload: dict, status: int = 200) -> None:
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route == "/__admin/api/state":
                try:
                    with ADMIN_LOCK:
                        self.send_json(application.state())
                except Exception as error:
                    self.send_json({"error": str(error)}, 500)
                return
            if route in ("/", "/index.html"):
                content = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
                content = content.replace(
                    "</head>",
                    '<link rel="stylesheet" href="/__admin/admin.css">\n</head>',
                ).replace(
                    "</body>",
                    '<script defer src="/__admin/admin.js"></script>\n</body>',
                )
                self.send_bytes(content.encode("utf-8"), "text/html; charset=utf-8")
                return
            file_path = _safe_file(route)
            if file_path is None:
                self.send_error(404)
                return
            content_type, _encoding = mimetypes.guess_type(file_path.name)
            self.send_bytes(
                file_path.read_bytes(),
                content_type or "application/octet-stream",
            )

        def do_POST(self) -> None:
            expected_origin = f"http://127.0.0.1:{port}"
            if (
                self.headers.get("Origin") != expected_origin
                or self.headers.get("Host") != f"127.0.0.1:{port}"
            ):
                self.send_json({"error": "只接受本机管理页面发起的请求。"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("请求内容为空或超过限制。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求内容必须是 JSON 对象。")
                route = urlsplit(self.path).path
                with ADMIN_LOCK:
                    if route == "/__admin/api/merge":
                        application.merge(payload)
                        response = {"message": "合并已保存，可在本地预览。"}
                    elif route == "/__admin/api/sync":
                        response = application.sync_from_pages()
                    elif route == "/__admin/api/cleanup":
                        removed = cleanup_archives()
                        response = {
                            "message": f"已手动清理 {len(removed)} 个非主体原始归档。",
                            "removed": removed,
                        }
                    elif route == "/__admin/api/publish":
                        response = application.publish()
                    else:
                        self.send_json({"error": "未知管理操作。"}, 404)
                        return
                self.send_json(response)
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, 400)
            except subprocess.CalledProcessError as error:
                details = (error.stderr or error.stdout or str(error)).strip()
                self.send_json({"error": details}, 500)
            except Exception as error:
                self.send_json({"error": str(error)}, 500)

        def send_bytes(self, content: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the local-only Bilibili Banner archive manager."
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    application = AdminApplication()
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(application, args.port),
    )
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Local Banner manager: {url}")
    print("Only this computer can connect. Press Ctrl+C to stop.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local Banner manager.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
