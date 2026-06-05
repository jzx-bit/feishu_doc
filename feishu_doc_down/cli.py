from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlparse

import requests


ONLINE_TYPES = {"doc", "docx", "sheet", "bitable"}
FOLDER_TYPES = {"folder"}
REGULAR_FILE_TYPES = {"file"}
SKIP_TYPES = {"mindnote", "slides"}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
URL_TYPE_BY_PATH_PREFIX = {
    "docx": "docx",
    "doc": "doc",
    "sheets": "sheet",
    "base": "bitable",
    "bitable": "bitable",
}
DEFAULT_SCOPE = (
    "auth:user.id:read "
    "drive:drive "
    "drive:drive.metadata:readonly "
    "drive:drive:readonly "
    "search:docs:read "
    "wiki:wiki:readonly "
    "wiki:node:retrieve "
    "calendar:calendar:read "
    "calendar:calendar.event:read "
    "vc:meeting.search:read "
    "vc:record:readonly "
    "minutes:minutes.media:export "
    "drive:file:download "
    "docs:document:export "
    "offline_access"
)
TOKEN_EXPIRY_SKEW_SECONDS = 120


class FeishuError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadStats:
    exported: int = 0
    downloaded: int = 0
    planned: int = 0
    skipped: int = 0
    failed: int = 0

    def add(self, **changes: int) -> "DownloadStats":
        values = {
            "exported": self.exported,
            "downloaded": self.downloaded,
            "planned": self.planned,
            "skipped": self.skipped,
            "failed": self.failed,
        }
        for key, value in changes.items():
            values[key] += value
        return DownloadStats(**values)


class FeishuClient:
    def __init__(self, token: str, base_url: str, timeout: int) -> None:
        if not token or not token.strip():
            raise FeishuError("empty access token; run `feishu-doc-down auth status`")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "feishu-doc-down/0.1.0",
            }
        )

    def get_root_folder_token(self) -> str:
        data = self._request_json("GET", "/open-apis/drive/explorer/v2/root_folder/meta")
        token = data.get("token")
        if not token:
            raise FeishuError("root folder response does not contain data.token")
        return token

    def list_files(self, folder_token: str | None, page_size: int) -> Iterable[dict[str, Any]]:
        page_token = None
        while True:
            params: dict[str, Any] = {
                "page_size": page_size,
                "order_by": "EditedTime",
                "direction": "DESC",
            }
            if folder_token:
                params["folder_token"] = folder_token
            if page_token:
                params["page_token"] = page_token

            data = self._request_json("GET", "/open-apis/drive/v1/files", params=params)
            for item in data.get("files", []):
                yield item

            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or data.get("next_page_token")
            if not page_token:
                raise FeishuError("list files returned has_more=true without page_token")

    def list_explorer_children(self, folder_token: str) -> Iterable[dict[str, Any]]:
        data = self._request_json("GET", f"/open-apis/drive/explorer/v2/folder/{folder_token}/children")
        children = data.get("children") or {}
        if isinstance(children, dict):
            yield from children.values()
            return
        if isinstance(children, list):
            yield from children

    def list_wiki_nodes(
        self,
        space_id: str,
        parent_node_token: str | None,
        page_size: int,
    ) -> Iterable[dict[str, Any]]:
        page_token = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if parent_node_token:
                params["parent_node_token"] = parent_node_token
            if page_token:
                params["page_token"] = page_token

            data = self._request_json("GET", f"/open-apis/wiki/v2/spaces/{space_id}/nodes", params=params)
            for item in data.get("items", []):
                yield item

            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                raise FeishuError("list wiki nodes returned has_more=true without page_token")

    def search_docs(self, search_key: str, count: int) -> Iterable[dict[str, Any]]:
        offset = 0
        seen_tokens: set[str] = set()
        while True:
            payload = {"search_key": search_key, "count": count, "offset": offset}
            data = self._request_json("POST", "/open-apis/suite/docs-api/search/object", json=payload)
            entities = data.get("docs_entities", [])
            for entity in entities:
                token = entity.get("docs_token")
                if token and token in seen_tokens:
                    continue
                if token:
                    seen_tokens.add(token)
                yield {
                    "token": token,
                    "type": entity.get("docs_type"),
                    "name": entity.get("title") or token or "untitled",
                    "url": entity.get("url"),
                    "owner_id": entity.get("owner_id"),
                    "search_entity": entity,
                }

            if not data.get("has_more"):
                break
            offset += len(entities) or count

    def get_primary_calendar(self) -> dict[str, Any]:
        data = self._request_json("POST", "/open-apis/calendar/v4/calendars/primary")
        return data.get("calendar") or data

    def list_calendars(self, page_size: int) -> Iterable[dict[str, Any]]:
        page_token = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json("GET", "/open-apis/calendar/v4/calendars", params=params)
            for item in data.get("calendar_list", []) or data.get("items", []):
                yield item
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                raise FeishuError("list calendars returned has_more=true without page_token")

    def list_calendar_events(
        self,
        calendar_id: str,
        start_ts: int,
        end_ts: int,
        page_size: int,
    ) -> Iterable[dict[str, Any]]:
        params = {
            "start_time": str(start_ts),
            "end_time": str(end_ts),
            "page_size": page_size,
        }
        data = self._request_json("GET", f"/open-apis/calendar/v4/calendars/{calendar_id}/events", params=params)
        for item in data.get("items", []):
            yield item

    def search_meetings(
        self,
        query: str,
        start_time: str,
        end_time: str,
        page_size: int,
    ) -> Iterable[dict[str, Any]]:
        page_token = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token

            payload: dict[str, Any] = {}
            if query:
                payload["query"] = query
            meeting_filter: dict[str, Any] = {}
            if start_time:
                meeting_filter["start_time"] = {"gte": start_time}
            if end_time:
                meeting_filter["end_time"] = {"lte": end_time}
            if meeting_filter:
                payload["meeting_filter"] = meeting_filter

            data = self._request_json("POST", "/open-apis/vc/v1/meetings/search", params=params, json=payload)
            for item in data.get("items", []):
                yield item

            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                raise FeishuError("search meetings returned has_more=true without page_token")

    def get_meeting_recording(self, meeting_id: str) -> dict[str, Any]:
        data = self._request_json("GET", f"/open-apis/vc/v1/meetings/{meeting_id}/recording")
        return data.get("recording") or {}

    def get_minutes_media_download_url(self, minute_token: str) -> str:
        data = self._request_json("GET", f"/open-apis/minutes/v1/minutes/{minute_token}/media")
        download_url = data.get("download_url")
        if not download_url:
            raise FeishuError("minutes media response does not contain data.download_url")
        return str(download_url)

    def create_export_task(self, token: str, file_type: str, extension: str) -> str:
        payload = {"token": token, "type": file_type, "file_extension": extension}
        data = self._request_json("POST", "/open-apis/drive/v1/export_tasks", json=payload)
        ticket = data.get("ticket")
        if not ticket:
            raise FeishuError("export task response does not contain data.ticket")
        return ticket

    def wait_export_task(
        self,
        ticket: str,
        source_token: str,
        poll_interval: float,
        export_timeout: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + export_timeout
        last_result: dict[str, Any] = {}

        while time.monotonic() < deadline:
            data = self._request_json(
                "GET",
                f"/open-apis/drive/v1/export_tasks/{ticket}",
                params={"token": source_token},
            )
            result = data.get("result") or {}
            last_result = result

            if result.get("file_token"):
                return result

            error_msg = result.get("job_error_msg")
            if error_msg and error_msg != "success":
                raise FeishuError(f"export task failed: {error_msg}")

            time.sleep(poll_interval)

        raise FeishuError(f"export task timed out: ticket={ticket}, last_result={last_result}")

    def download_exported_file(self, file_token: str, output_path: Path) -> None:
        self._download(f"/open-apis/drive/v1/export_tasks/file/{file_token}/download", output_path)

    def download_regular_file(self, file_token: str, output_path: Path) -> None:
        self._download(f"/open-apis/drive/v1/files/{file_token}/download", output_path)

    def download_external_url(self, url: str, output_path: Path) -> None:
        with requests.get(url, timeout=self.timeout, stream=True) as response:
            if response.status_code >= 400:
                raise FeishuError(f"download failed: HTTP {response.status_code} {response.text[:500]}")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = output_path.with_suffix(output_path.suffix + ".part")
            with tmp_path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(output_path)

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        try:
            body = response.json()
        except ValueError:
            response.raise_for_status()
            raise FeishuError(f"{method} {path} returned non-JSON response: HTTP {response.status_code}")
        if response.status_code >= 400 and not isinstance(body, dict):
            raise FeishuError(f"{method} {path} failed: HTTP {response.status_code} {body}")
        if body.get("code") != 0:
            if body.get("code") == 99991661:
                raise FeishuError(
                    f"{method} {path} failed: missing access token. "
                    "Run `feishu-doc-down auth status` and re-run `feishu-doc-down auth` if token is missing."
                )
            raise FeishuError(
                f"{method} {path} failed: HTTP {response.status_code} "
                f"code={body.get('code')} msg={body.get('msg')}"
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _download(self, path: str, output_path: Path) -> None:
        url = f"{self.base_url}{path}"
        with self.session.get(url, timeout=self.timeout, stream=True) as response:
            if response.status_code >= 400:
                raise FeishuError(f"download failed: HTTP {response.status_code} {response.text[:500]}")

            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                body = response.json()
                if body.get("code") != 0:
                    raise FeishuError(f"download failed: code={body.get('code')} msg={body.get('msg')}")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = output_path.with_suffix(output_path.suffix + ".part")
            with tmp_path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(output_path)


def parse_download_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="feishu-doc-down",
        description="Download/export documents from the current Feishu user's My Space.",
    )
    parser.add_argument("output_dir", type=Path, help="local folder to save downloaded files")
    parser.add_argument(
        "--token",
        default=os.getenv("FEISHU_USER_ACCESS_TOKEN") or os.getenv("LARK_USER_ACCESS_TOKEN"),
        help="Feishu user_access_token. Defaults to env vars, then saved auth config.",
    )
    parser.add_argument("--config", type=Path, default=default_config_path(), help="saved auth config path")
    parser.add_argument("--base-url", default="https://open.feishu.cn", help="OpenAPI base URL")
    parser.add_argument(
        "--source",
        choices=["my-library", "explorer", "folder", "search", "all"],
        default="my-library",
        help="my-library downloads the Docs sidebar library; explorer/folder use Drive v1; search downloads searchable docs",
    )
    parser.add_argument("--url", action="append", default=[], help="download a specific Feishu document URL; repeatable")
    parser.add_argument("--search-key", default="", help="keyword for --source search. Empty means all searchable docs.")
    parser.add_argument("--search-count", type=int, default=50, help="page size for --source search")
    parser.add_argument("--root-folder-token", help="start from a specific folder token instead of My Space root")
    parser.add_argument("--doc-format", choices=["docx", "pdf"], default="docx")
    parser.add_argument("--sheet-format", choices=["xlsx", "csv"], default="xlsx")
    parser.add_argument("--bitable-format", choices=["xlsx", "csv"], default="xlsx")
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=60, help="HTTP request timeout in seconds")
    parser.add_argument("--export-timeout", type=int, default=600, help="export task timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="export task polling interval")
    parser.add_argument("--skip-existing", action="store_true", help="do not overwrite existing local files")
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without downloading")
    parser.add_argument("--include-shortcuts", action="store_true", help="try downloading shortcut targets")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="optional JSONL manifest path. Defaults to <output_dir>/manifest.jsonl.",
    )
    return parser.parse_args(argv)


def parse_auth_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="feishu-doc-down auth",
        description="Authorize a Feishu user with OAuth and save user_access_token locally.",
    )
    parser.add_argument("--app-id", default=os.getenv("FEISHU_APP_ID") or os.getenv("LARK_APP_ID"), help="Feishu App ID")
    parser.add_argument(
        "--app-secret",
        default=os.getenv("FEISHU_APP_SECRET") or os.getenv("LARK_APP_SECRET"),
        help="Feishu App Secret",
    )
    parser.add_argument("--base-url", default="https://open.feishu.cn", help="OpenAPI base URL")
    parser.add_argument(
        "--auth-url",
        default="https://accounts.feishu.cn/open-apis/authen/v1/authorize",
        help="OAuth authorization URL",
    )
    parser.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback", help="registered OAuth callback URL")
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help="space-separated OAuth scopes")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="saved auth config path")
    parser.add_argument("--app-config", type=Path, default=None, help="saved app credential config path")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for browser callback")
    parser.add_argument("--no-browser", action="store_true", help="do not try to open the authorization URL")
    parser.add_argument("--no-qr", action="store_true", help="do not print terminal QR code")
    args = parser.parse_args(argv)
    apply_saved_app_credentials(args)
    return args


def parse_menu_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="feishu-doc-down menu",
        description="Interactive downloader menu.",
    )
    parser.add_argument("output_dir", nargs="?", type=Path, help="local folder to save downloaded files")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="saved auth config path")
    parser.add_argument("--base-url", default="https://open.feishu.cn", help="OpenAPI base URL")
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without downloading")
    parser.add_argument("--skip-existing", action="store_true", help="do not overwrite existing local files")
    return parser.parse_args(argv)


def parse_calendar_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="feishu-doc-down calendar",
        description="Export Feishu calendar events.",
    )
    parser.add_argument("output", type=Path, help="output file path, or directory when exporting all calendars")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="saved auth config path")
    parser.add_argument("--base-url", default="https://open.feishu.cn", help="OpenAPI base URL")
    parser.add_argument("--calendar-id", default="primary", help="calendar ID, or primary")
    parser.add_argument("--all-calendars", action="store_true", help="export all readable calendars into a directory")
    parser.add_argument("--start", required=True, help="start datetime, for example 2026-05-01 or 2026-05-01T00:00:00+08:00")
    parser.add_argument("--end", required=True, help="end datetime, for example 2026-06-01 or 2026-06-01T00:00:00+08:00")
    parser.add_argument("--format", choices=["json", "csv", "ics"], default="csv")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=60, help="HTTP request timeout in seconds")
    return parser.parse_args(argv)


def parse_recording_args(argv: list[str] | None = None) -> argparse.Namespace:
    today = datetime.now().astimezone().date()
    parser = argparse.ArgumentParser(
        prog="feishu-doc-down recordings",
        description="Download Feishu meeting recording media files.",
    )
    parser.add_argument("output_dir", type=Path, help="local folder to save recording files")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="saved auth config path")
    parser.add_argument("--base-url", default="https://open.feishu.cn", help="OpenAPI base URL")
    parser.add_argument("--query", default="", help="meeting search keyword")
    parser.add_argument(
        "--start",
        default=(today - timedelta(days=90)).isoformat(),
        help="search start datetime/date, default is 90 days ago",
    )
    parser.add_argument(
        "--end",
        default=(today + timedelta(days=1)).isoformat(),
        help="search end datetime/date, default is tomorrow",
    )
    parser.add_argument("--meeting-id", action="append", default=[], help="specific meeting ID; repeatable")
    parser.add_argument("--minute-token", action="append", default=[], help="specific Feishu Minutes token; repeatable")
    parser.add_argument("--minute-url", action="append", default=[], help="specific Feishu Minutes URL; repeatable")
    parser.add_argument("--page-size", type=int, default=30, help="meeting search page size, max 30")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP request timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without downloading")
    parser.add_argument("--skip-existing", action="store_true", help="do not overwrite existing local files")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="optional JSONL manifest path. Defaults to <output_dir>/recordings-manifest.jsonl.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    program_name = Path(sys.argv[0]).stem.lower()
    if raw_args and raw_args[0] == "gui":
        return gui_main(raw_args[1:])
    if "gui" in program_name and (not raw_args or raw_args[0] not in {"auth", "menu", "interactive", "calendar", "download"}):
        return gui_main(raw_args)
    if raw_args and raw_args[0] == "auth":
        if len(raw_args) > 1 and raw_args[1] == "app-config":
            return auth_app_config_main(raw_args[2:])
        if len(raw_args) > 1 and raw_args[1] == "status":
            return auth_status_main(raw_args[2:])
        if len(raw_args) > 1 and raw_args[1] == "whoami":
            return auth_whoami_main(raw_args[2:])
        return auth_main(raw_args[1:])
    if raw_args and raw_args[0] in {"menu", "interactive"}:
        return menu_main(raw_args[1:])
    if raw_args and raw_args[0] == "calendar":
        return calendar_main(raw_args[1:])
    if raw_args and raw_args[0] in {"recordings", "recording"}:
        return recordings_main(raw_args[1:])
    if raw_args and raw_args[0] == "download":
        return download_main(raw_args[1:])
    return download_main(raw_args)


def gui_main(argv: list[str] | None = None) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print("usage: feishu-doc-down gui")
        print("Open the Feishu Doc Down desktop window.")
        return 0

    try:
        import contextlib
        import threading
        import tkinter as tk
        from datetime import date, timedelta
        from tkinter import filedialog, messagebox, ttk
        from tkinter import font as tkfont
    except Exception as exc:
        print(f"error: GUI requires tkinter: {exc}", file=sys.stderr)
        return 1

    root = tk.Tk()
    root.title("Feishu Doc Down")
    root.geometry("860x640")
    root.minsize(760, 540)

    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")
    gui_font_family = configure_gui_fonts(root, style, tkfont)

    output_var = tk.StringVar(value=str(Path("./downloads")))
    source_var = tk.StringVar(value="all")
    dry_run_var = tk.BooleanVar(value=False)
    skip_existing_var = tk.BooleanVar(value=True)
    keyword_var = tk.StringVar()
    calendar_format_var = tk.StringVar(value="csv")
    all_calendars_var = tk.BooleanVar(value=False)
    today = date.today()
    default_end = today + timedelta(days=1)
    default_start = today.replace(day=1)
    calendar_start_var = tk.StringVar(value=default_start.isoformat())
    calendar_end_var = tk.StringVar(value=default_end.isoformat())
    running_var = tk.BooleanVar(value=False)

    root.columnconfigure(0, weight=1)
    root.rowconfigure(3, weight=1)

    header = ttk.Frame(root, padding=(16, 14, 16, 6))
    header.grid(row=0, column=0, sticky="ew")
    header.columnconfigure(1, weight=1)
    ttk.Label(header, text="Feishu Doc Down", style="Title.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(header, text="授权后选择来源，下载飞书云文档、云盘文件或日历日程。").grid(
        row=1, column=0, columnspan=3, sticky="w", pady=(4, 0)
    )

    output_frame = ttk.Frame(root, padding=(16, 8, 16, 6))
    output_frame.grid(row=1, column=0, sticky="ew")
    output_frame.columnconfigure(1, weight=1)
    ttk.Label(output_frame, text="保存目录").grid(row=0, column=0, sticky="w", padx=(0, 8))
    ttk.Entry(output_frame, textvariable=output_var).grid(row=0, column=1, sticky="ew")

    def browse_output() -> None:
        selected = filedialog.askdirectory(initialdir=output_var.get() or ".")
        if selected:
            output_var.set(selected)

    ttk.Button(output_frame, text="选择", command=browse_output).grid(row=0, column=2, sticky="e", padx=(8, 0))

    main_frame = ttk.Frame(root, padding=(16, 8, 16, 8))
    main_frame.grid(row=2, column=0, sticky="ew")
    main_frame.columnconfigure(1, weight=1)

    source_frame = ttk.LabelFrame(main_frame, text="下载来源", padding=10)
    source_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
    source_options = [
        ("全部", "all"),
        ("云盘", "explorer"),
        ("我的文档库", "my-library"),
        ("搜索", "search"),
        ("按链接", "url"),
        ("日历日程", "calendar"),
        ("会议录制", "recordings"),
    ]
    for row, (label, value) in enumerate(source_options):
        ttk.Radiobutton(source_frame, text=label, value=value, variable=source_var).grid(row=row, column=0, sticky="w")

    options_frame = ttk.LabelFrame(main_frame, text="参数", padding=10)
    options_frame.grid(row=0, column=1, sticky="nsew")
    options_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(options_frame, text="先预览，不实际下载", variable=dry_run_var).grid(
        row=0, column=0, columnspan=2, sticky="w"
    )
    ttk.Checkbutton(options_frame, text="跳过已存在文件", variable=skip_existing_var).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(4, 8)
    )
    ttk.Label(options_frame, text="搜索关键词").grid(row=2, column=0, sticky="w", padx=(0, 8))
    ttk.Entry(options_frame, textvariable=keyword_var).grid(row=2, column=1, sticky="ew")

    ttk.Label(options_frame, text="文档链接").grid(row=3, column=0, sticky="nw", padx=(0, 8), pady=(8, 0))
    url_text = tk.Text(options_frame, height=4, wrap="word", font="TkTextFont")
    url_text.grid(row=3, column=1, sticky="ew", pady=(8, 0))

    ttk.Label(options_frame, text="日历开始").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
    ttk.Entry(options_frame, textvariable=calendar_start_var).grid(row=4, column=1, sticky="ew", pady=(8, 0))
    ttk.Label(options_frame, text="日历结束").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
    ttk.Entry(options_frame, textvariable=calendar_end_var).grid(row=5, column=1, sticky="ew", pady=(6, 0))
    ttk.Label(options_frame, text="日历格式").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
    ttk.Combobox(
        options_frame,
        textvariable=calendar_format_var,
        values=("csv", "json", "ics"),
        state="readonly",
        width=8,
    ).grid(row=6, column=1, sticky="w", pady=(6, 0))
    ttk.Checkbutton(options_frame, text="导出全部可读日历", variable=all_calendars_var).grid(
        row=7, column=1, sticky="w", pady=(6, 0)
    )

    log_frame = ttk.LabelFrame(root, text="运行日志", padding=8)
    log_frame.grid(row=3, column=0, sticky="nsew", padx=16, pady=(4, 10))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)
    log_text = tk.Text(log_frame, height=12, wrap="word", font="TkTextFont")
    log_text.grid(row=0, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=scrollbar.set)

    actions = ttk.Frame(root, padding=(16, 0, 16, 16))
    actions.grid(row=4, column=0, sticky="ew")
    actions.columnconfigure(2, weight=1)

    def append_log(text: str) -> None:
        log_text.insert("end", text)
        log_text.see("end")

    class TkLogWriter:
        def write(self, value: str) -> int:
            if value:
                root.after(0, append_log, value)
            return len(value)

        def flush(self) -> None:
            return

    def set_running(is_running: bool) -> None:
        running_var.set(is_running)
        state = "disabled" if is_running else "normal"
        auth_button.configure(state=state)
        start_button.configure(state=state)

    def run_background(title: str, target: Any) -> None:
        if running_var.get():
            messagebox.showinfo("正在运行", "当前任务还没有结束。")
            return

        set_running(True)
        append_log(f"\n== {title} ==\n")

        def worker() -> None:
            code = 1
            writer = TkLogWriter()
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    code = target()
                    print(f"exit code: {code}")
            except Exception as exc:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    print(f"error: {exc}", file=sys.stderr)
            finally:
                root.after(0, set_running, False)

        threading.Thread(target=worker, daemon=True).start()

    def collect_job() -> tuple[str, list[str]] | None:
        output_dir = output_var.get().strip() or "./downloads"
        source = source_var.get()
        if source == "calendar":
            start = calendar_start_var.get().strip()
            end = calendar_end_var.get().strip()
            if not start or not end:
                messagebox.showerror("参数缺失", "日历导出需要填写开始和结束时间。")
                return None
            export_format = calendar_format_var.get()
            output_path = Path(output_dir) if all_calendars_var.get() else Path(output_dir) / f"calendar-events.{export_format}"
            args = [
                str(output_path),
                "--start",
                start,
                "--end",
                end,
                "--format",
                export_format,
            ]
            if all_calendars_var.get():
                args.append("--all-calendars")
            return "导出日历", args

        if source == "recordings":
            start = calendar_start_var.get().strip()
            end = calendar_end_var.get().strip()
            if not start or not end:
                messagebox.showerror("参数缺失", "会议录制导出需要填写开始和结束时间。")
                return None
            args = [output_dir, "--start", start, "--end", end]
            minute_urls = [line.strip() for line in url_text.get("1.0", "end").splitlines() if line.strip()]
            for minute_url in minute_urls:
                args.extend(["--minute-url", minute_url])
            query = keyword_var.get().strip()
            if query:
                args.extend(["--query", query])
            if dry_run_var.get():
                args.append("--dry-run")
            if skip_existing_var.get():
                args.append("--skip-existing")
            return "下载会议录制", args

        args = [output_dir]
        if dry_run_var.get():
            args.append("--dry-run")
        if skip_existing_var.get():
            args.append("--skip-existing")

        if source == "url":
            urls = [line.strip() for line in url_text.get("1.0", "end").splitlines() if line.strip()]
            if not urls:
                messagebox.showerror("参数缺失", "按链接下载需要至少填写一个文档链接。")
                return None
            for url in urls:
                args.extend(["--url", url])
        else:
            args.extend(["--source", source])
            if source == "search":
                args.extend(["--search-key", keyword_var.get().strip()])

        return "开始下载", args

    def start_auth() -> None:
        run_background("授权", lambda: auth_main(["--no-qr"]))

    def start_download() -> None:
        job = collect_job()
        if not job:
            return
        title, args = job
        if source_var.get() == "calendar":
            target = calendar_main
        elif source_var.get() == "recordings":
            target = recordings_main
        else:
            target = download_main
        run_background(title, lambda: target(args))

    auth_button = ttk.Button(actions, text="授权", command=start_auth)
    auth_button.grid(row=0, column=0, sticky="w")
    start_button = ttk.Button(actions, text="开始", command=start_download)
    start_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
    ttk.Button(actions, text="退出", command=root.destroy).grid(row=0, column=3, sticky="e")

    font_resize_after: list[Any] = [None]

    def schedule_font_resize(event: Any) -> None:
        if event.widget is not root:
            return
        if font_resize_after[0]:
            root.after_cancel(font_resize_after[0])
        font_resize_after[0] = root.after(80, resize_gui_fonts)

    def resize_gui_fonts() -> None:
        font_resize_after[0] = None
        width = max(root.winfo_width(), 1)
        height = max(root.winfo_height(), 1)
        scale = min(width / 860, height / 640)
        apply_gui_font_scale(root, style, tkfont, gui_font_family, scale)

    root.bind("<Configure>", schedule_font_resize)
    append_log("先点“授权”完成飞书登录，再选择来源并点“开始”。\n")
    root.mainloop()
    return 0


def configure_gui_fonts(root: Any, style: Any, tkfont: Any) -> str:
    available = {name.casefold(): name for name in tkfont.families(root)}
    candidates = gui_font_candidates()
    default_family = tkfont.nametofont("TkDefaultFont").actual("family")
    family = next((available[name.casefold()] for name in candidates if name.casefold() in available), default_family)

    root.option_add("*Font", (family, 10))
    apply_gui_font_scale(root, style, tkfont, family, 1.0)
    return family


def apply_gui_font_scale(root: Any, style: Any, tkfont: Any, family: str, scale: float) -> None:
    normal_size = clamp_int(round(10 * scale), 9, 16)
    small_size = clamp_int(round(9 * scale), 8, 14)
    title_size = clamp_int(round(16 * scale), 14, 26)
    font_specs = {
        "TkDefaultFont": {"family": family, "size": normal_size},
        "TkTextFont": {"family": family, "size": normal_size},
        "TkMenuFont": {"family": family, "size": normal_size},
        "TkHeadingFont": {"family": family, "size": normal_size, "weight": "bold"},
        "TkCaptionFont": {"family": family, "size": normal_size},
        "TkSmallCaptionFont": {"family": family, "size": small_size},
        "TkIconFont": {"family": family, "size": normal_size},
        "TkTooltipFont": {"family": family, "size": small_size},
    }
    for name, spec in font_specs.items():
        try:
            tkfont.nametofont(name).configure(**spec)
        except Exception:
            continue

    default_font = (family, normal_size)
    root.option_add("*Font", default_font)
    style.configure(".", font=default_font)
    style.configure("TLabel", font=default_font)
    style.configure("TButton", font=default_font)
    style.configure("TEntry", font=default_font)
    style.configure("TCombobox", font=default_font)
    style.configure("TRadiobutton", font=default_font)
    style.configure("TCheckbutton", font=default_font)
    style.configure("TLabelframe.Label", font=(family, normal_size, "bold"))
    style.configure("Title.TLabel", font=(family, title_size, "bold"))


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def gui_font_candidates() -> list[str]:
    common = [
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "WenQuanYi Micro Hei",
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "Hiragino Sans GB",
        "SimHei",
        "Arial Unicode MS",
    ]
    if sys.platform.startswith("win"):
        return ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "NSimSun", *common]
    if sys.platform == "darwin":
        return ["PingFang SC", "Hiragino Sans GB", "Heiti SC", *common]
    return ["Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "Noto Sans CJK JP", *common]


def menu_main(argv: list[str] | None = None) -> int:
    args = parse_menu_args(argv)

    print("Feishu Doc Down")
    print("1. 云盘")
    print("2. 我的文档库")
    print("3. 全部")
    print("4. 搜索")
    print("5. 按链接下载")
    print("6. 导出日历日程")
    print("7. 下载会议录制")
    choice = prompt_choice("请选择下载来源 [1-7]", {"1", "2", "3", "4", "5", "6", "7"}, "3")

    output_dir = args.output_dir or Path(prompt_text("保存到哪个文件夹", "./downloads"))
    if choice == "6":
        calendar_format = prompt_choice("导出格式 [csv/json/ics]", {"csv", "json", "ics"}, "csv")
        start = prompt_text("开始时间，例如 2026-05-01", "")
        end = prompt_text("结束时间，例如 2026-06-01", "")
        all_calendars = prompt_yes_no("导出全部日历吗", False)
        calendar_args = [
            str(output_dir / f"calendar-events.{calendar_format}" if not all_calendars else output_dir),
            "--config",
            str(args.config),
            "--base-url",
            args.base_url,
            "--format",
            calendar_format,
            "--start",
            start,
            "--end",
            end,
        ]
        if all_calendars:
            calendar_args.append("--all-calendars")
        return calendar_main(calendar_args)

    if choice == "7":
        start = prompt_text("开始时间，例如 2026-05-01", (datetime.now().astimezone().date() - timedelta(days=90)).isoformat())
        end = prompt_text("结束时间，例如 2026-06-01", (datetime.now().astimezone().date() + timedelta(days=1)).isoformat())
        keyword = prompt_text("搜索关键词，可直接回车跳过", "")
        minute_urls = []
        while True:
            minute_url = prompt_text("粘贴妙记链接，直接回车结束", "")
            if not minute_url:
                break
            minute_urls.append(minute_url)
        recording_args = [
            str(output_dir),
            "--config",
            str(args.config),
            "--base-url",
            args.base_url,
            "--start",
            start,
            "--end",
            end,
        ]
        if keyword:
            recording_args += ["--query", keyword]
        for minute_url in minute_urls:
            recording_args += ["--minute-url", minute_url]
        if args.dry_run or prompt_yes_no("先 dry-run 预览，不实际下载吗", False):
            recording_args.append("--dry-run")
        if args.skip_existing or prompt_yes_no("跳过已存在文件吗", True):
            recording_args.append("--skip-existing")
        return recordings_main(recording_args)

    download_args = [
        str(output_dir),
        "--config",
        str(args.config),
        "--base-url",
        args.base_url,
    ]

    if args.dry_run or prompt_yes_no("先 dry-run 预览，不实际下载吗", False):
        download_args.append("--dry-run")
    if args.skip_existing or prompt_yes_no("跳过已存在文件吗", True):
        download_args.append("--skip-existing")

    if choice == "1":
        download_args += ["--source", "explorer"]
    elif choice == "2":
        download_args += ["--source", "my-library"]
    elif choice == "3":
        download_args += ["--source", "all"]
    elif choice == "4":
        keyword = prompt_text("搜索关键词", "")
        download_args += ["--source", "search", "--search-key", keyword]
    else:
        while True:
            url = prompt_text("粘贴文档链接，直接回车结束", "")
            if not url:
                break
            download_args += ["--url", url]
        if "--url" not in download_args:
            print("没有输入链接，已取消。")
            return 2

    return download_main(download_args)


def prompt_choice(prompt: str, choices: set[str], default: str) -> str:
    while True:
        value = input(f"{prompt} 默认 {default}: ").strip() or default
        if value in choices:
            return value
        print(f"请输入: {', '.join(sorted(choices))}")


def prompt_text(prompt: str, default: str) -> str:
    suffix = f" 默认 {default}" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def prompt_yes_no(prompt: str, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{default_text}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "1", "true"}


def download_main(argv: list[str] | None = None) -> int:
    args = parse_download_args(argv)
    if args.page_size < 1 or args.page_size > 200:
        print("error: --page-size must be between 1 and 200", file=sys.stderr)
        return 2
    if args.search_count < 1 or args.search_count > 50:
        print("error: --search-count must be between 1 and 50", file=sys.stderr)
        return 2

    try:
        args.token = resolve_access_token(args)
    except FeishuError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = FeishuClient(args.token, args.base_url, args.timeout)
    manifest_path = args.manifest or args.output_dir / "manifest.jsonl"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with manifest_path.open("a", encoding="utf-8") as manifest:
            if args.url:
                stats = walk_urls(client, args, manifest, args.output_dir)
            elif args.source == "my-library":
                stats = walk_my_library(client, args, manifest, args.output_dir, set())
            elif args.source == "all":
                stats = walk_all_sources(client, args, manifest, args.output_dir)
            elif args.source == "folder":
                root_token = args.root_folder_token or client.get_root_folder_token()
                stats = walk_folder(client, args, manifest, root_token, args.output_dir, [], set())
            elif args.source == "explorer":
                root_token = args.root_folder_token or client.get_root_folder_token()
                stats = walk_folder(client, args, manifest, root_token, args.output_dir, [], set())
            else:
                stats = walk_search(client, args, manifest, args.output_dir)
    except (requests.RequestException, FeishuError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        "done: "
        f"exported={stats.exported}, downloaded={stats.downloaded}, "
        f"planned={stats.planned}, skipped={stats.skipped}, failed={stats.failed}"
    )
    return 0 if stats.failed == 0 else 1


def calendar_main(argv: list[str] | None = None) -> int:
    args = parse_calendar_args(argv)
    if args.page_size < 1 or args.page_size > 1000:
        print("error: --page-size must be between 1 and 1000", file=sys.stderr)
        return 2

    try:
        token = resolve_access_token(args)
        client = FeishuClient(token, args.base_url, args.timeout)
        start_ts = parse_datetime_to_epoch(args.start)
        end_ts = parse_datetime_to_epoch(args.end)
        if end_ts <= start_ts:
            raise FeishuError("--end must be after --start")

        if args.all_calendars:
            args.output.mkdir(parents=True, exist_ok=True)
            total = 0
            for calendar in client.list_calendars(args.page_size):
                calendar_id = calendar.get("calendar_id")
                if not calendar_id:
                    continue
                events = list(client.list_calendar_events(calendar_id, start_ts, end_ts, args.page_size))
                name = sanitize_filename(calendar.get("summary") or calendar_id)
                output_path = args.output / f"{name}.{args.format}"
                write_calendar_export(output_path, args.format, events, calendar)
                print(f"export calendar: {calendar.get('summary') or calendar_id} -> {output_path} ({len(events)} events)")
                total += len(events)
            print(f"done: calendars exported, events={total}")
            return 0

        calendar = client.get_primary_calendar() if args.calendar_id == "primary" else {"calendar_id": args.calendar_id}
        calendar_id = calendar.get("calendar_id") or args.calendar_id
        events = list(client.list_calendar_events(calendar_id, start_ts, end_ts, args.page_size))
        output_path = ensure_calendar_suffix(args.output, args.format)
        write_calendar_export(output_path, args.format, events, calendar)
        print(f"done: calendar={calendar_id}, events={len(events)}, output={output_path}")
        return 0
    except (requests.RequestException, FeishuError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def recordings_main(argv: list[str] | None = None) -> int:
    args = parse_recording_args(argv)
    if args.page_size < 1 or args.page_size > 30:
        print("error: --page-size must be between 1 and 30", file=sys.stderr)
        return 2

    try:
        token = resolve_access_token(args)
        client = FeishuClient(token, args.base_url, args.timeout)
        start_iso = parse_datetime_to_iso(args.start)
        end_iso = parse_datetime_to_iso(args.end)
        if datetime.fromisoformat(end_iso) <= datetime.fromisoformat(start_iso):
            raise FeishuError("--end must be after --start")

        args.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = args.manifest or args.output_dir / "recordings-manifest.jsonl"
        minute_entries = []
        for minute_url in args.minute_url:
            minute_token = extract_minute_token(minute_url)
            if not minute_token:
                raise FeishuError(f"cannot extract minute token from URL: {minute_url}")
            minute_entries.append((minute_token, minute_url))
        minute_entries.extend((token.strip(), None) for token in args.minute_token if token.strip())

        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        effective_query = args.query.strip() or "会议"
        meeting_items = (
            [{"id": meeting_id, "display_info": meeting_id, "meta_data": {}} for meeting_id in args.meeting_id]
            if args.meeting_id or minute_entries
            else client.search_meetings(effective_query, start_iso, end_iso, args.page_size)
        )

        stats = DownloadStats()
        seen_meeting_ids: set[str] = set()
        with manifest_path.open("a", encoding="utf-8") as manifest:
            for minute_token, minute_url in minute_entries:
                title = f"minutes-{minute_token}"
                output_path = args.output_dir / f"{sanitize_filename(title)}.mp4"
                stats = merge_stats(
                    stats,
                    download_minutes_media(client, args, manifest, None, title, minute_token, minute_url, output_path),
                )

            for item in meeting_items:
                meeting_id = str(item.get("id") or item.get("meeting_id") or "").strip()
                if not meeting_id or meeting_id in seen_meeting_ids:
                    continue
                seen_meeting_ids.add(meeting_id)
                if not args.meeting_id and not meeting_item_in_range(item, start_dt, end_dt):
                    continue
                title = meeting_title_from_search_item(item, meeting_id)
                output_path = args.output_dir / f"{sanitize_filename(title + '-' + meeting_id)}.mp4"

                try:
                    recording = client.get_meeting_recording(meeting_id)
                    recording_url = str(recording.get("url") or "")
                    minute_token = extract_minute_token(recording_url)
                    if not minute_token:
                        raise FeishuError("recording response does not contain a minutes URL")
                    stats = merge_stats(
                        stats,
                        download_minutes_media(
                            client,
                            args,
                            manifest,
                            meeting_id,
                            title,
                            minute_token,
                            recording_url,
                            output_path,
                        ),
                    )
                    time.sleep(0.25)
                except (requests.RequestException, FeishuError, OSError) as exc:
                    print(f"failed recording: {title} ({meeting_id}) ({exc})", file=sys.stderr)
                    stats = stats.add(failed=1)
                    write_recording_manifest(manifest, "failed", meeting_id, title, None, None, output_path, str(exc))

        print(
            "done: "
            f"recordings={stats.downloaded}, planned={stats.planned}, "
            f"skipped={stats.skipped}, failed={stats.failed}"
        )
        return 0 if stats.failed == 0 else 1
    except (requests.RequestException, FeishuError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def auth_main(argv: list[str] | None = None) -> int:
    args = parse_auth_args(argv)
    prompted = False
    if not args.app_id:
        args.app_id = prompt_text("请输入 Feishu App ID", "")
        prompted = True
    if not args.app_secret:
        args.app_secret = getpass.getpass("请输入 Feishu App Secret: ").strip()
        prompted = True
    if not args.app_id or not args.app_secret:
        print("error: missing App ID or App Secret", file=sys.stderr)
        return 2
    if prompted:
        save_app_config_from_args(args)
        print(f"saved app config to {args.app_config}")

    try:
        token_data = run_oauth_flow(args)
        save_token_config(args.config, args, token_data)
    except (FeishuError, OSError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    expires_in = token_data.get("expires_in")
    scope = token_data.get("scope")
    print(f"authorized: token saved to {args.config}")
    if expires_in:
        print(f"access token expires in about {expires_in} seconds")
    if scope:
        print(f"granted scope: {scope}")
    print("next: feishu-doc-down ./downloads")
    return 0


def auth_app_config_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feishu-doc-down auth app-config",
        description="Save Feishu App ID/Secret locally without committing them to source code.",
    )
    parser.add_argument("--app-id", required=True, help="Feishu App ID")
    parser.add_argument("--app-secret", required=True, help="Feishu App Secret")
    parser.add_argument("--base-url", default="https://open.feishu.cn", help="OpenAPI base URL")
    parser.add_argument("--auth-url", default="https://accounts.feishu.cn/open-apis/authen/v1/authorize")
    parser.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument("--app-config", type=Path, default=project_app_config_path(), help="saved app credential config path")
    args = parser.parse_args(argv)

    try:
        save_app_config_from_args(args)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"saved app config to {args.app_config}")
    print("next: feishu-doc-down auth")
    return 0


def save_app_config_from_args(args: argparse.Namespace) -> None:
    config = {
        "app_id": args.app_id,
        "app_secret": args.app_secret,
        "base_url": args.base_url,
        "auth_url": args.auth_url,
        "redirect_uri": args.redirect_uri,
        "scope": args.scope,
    }
    save_config_dict(args.app_config, config)


def walk_all_sources(
    client: FeishuClient,
    args: argparse.Namespace,
    manifest: Any,
    output_dir: Path,
) -> DownloadStats:
    stats = walk_my_library(client, args, manifest, output_dir / "my-library", set())

    root_token = args.root_folder_token or client.get_root_folder_token()
    stats = merge_stats(
        stats,
        walk_folder(client, args, manifest, root_token, output_dir / "drive", [], set()),
    )

    if args.search_key:
        stats = merge_stats(stats, walk_search(client, args, manifest, output_dir / "search"))

    return stats


def walk_my_library(
    client: FeishuClient,
    args: argparse.Namespace,
    manifest: Any,
    local_dir: Path,
    visited_nodes: set[str],
    parent_node_token: str | None = None,
    remote_path: list[str] | None = None,
) -> DownloadStats:
    stats = DownloadStats()
    remote_path = remote_path or []
    local_dir.mkdir(parents=True, exist_ok=True)

    if parent_node_token:
        if parent_node_token in visited_nodes:
            print(f"skip visited wiki node: {'/'.join(remote_path) or parent_node_token}")
            return stats.add(skipped=1)
        visited_nodes.add(parent_node_token)

    nodes = list(client.list_wiki_nodes("my_library", parent_node_token, min(args.page_size, 50)))
    folder_name_counts = count_folder_names(item_from_wiki_node(node) for node in nodes)

    for node in nodes:
        item = item_from_wiki_node(node)
        item_type = str(item.get("type") or "")
        item_name = str(item.get("name") or item.get("token") or "untitled")
        token = str(item.get("token") or "")
        safe_name = sanitize_filename(item_name)
        current_remote_path = remote_path + [item_name]

        node_token = str(node.get("node_token") or "")
        if item_type == "folder":
            next_dir = local_dir / disambiguate_folder_name(safe_name, node_token, folder_name_counts)
            print(f"folder: {'/'.join(current_remote_path)}")
            if not args.dry_run:
                next_dir.mkdir(parents=True, exist_ok=True)
            stats = merge_stats(
                stats,
                walk_my_library(client, args, manifest, next_dir, visited_nodes, node_token, current_remote_path),
            )
            continue

        try:
            result = handle_file(client, args, item, item_type, token, local_dir, safe_name, current_remote_path)
        except (requests.RequestException, FeishuError, OSError) as exc:
            print(f"failed: {'/'.join(current_remote_path)} ({exc})", file=sys.stderr)
            stats = stats.add(failed=1)
            write_manifest(manifest, "failed", item, current_remote_path, None, reason=str(exc))
            continue

        stats = stats.add(**{result["stat"]: 1})
        write_manifest(manifest, result["status"], item, current_remote_path, result.get("path"), reason=result.get("reason"))

    return stats


def item_from_wiki_node(node: dict[str, Any]) -> dict[str, Any]:
    item_type = node.get("obj_type") or node.get("type")
    token = node.get("obj_token") or node.get("token")
    return {
        "type": item_type,
        "token": token,
        "name": node.get("title") or node.get("name") or token or node.get("node_token") or "untitled",
        "url": node.get("url"),
        "wiki_node_token": node.get("node_token"),
        "wiki_node": node,
    }


def parse_datetime_to_iso(value: str) -> str:
    raw = value.strip()
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), timezone.utc).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raw = f"{raw}T00:00:00"
    normalized = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def parse_datetime_to_epoch(value: str) -> int:
    raw = value.strip()
    if raw.isdigit():
        return int(raw)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raw = f"{raw}T00:00:00"
    normalized = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp())


def meeting_title_from_search_item(item: dict[str, Any], fallback: str) -> str:
    display_info = str(item.get("display_info") or "").strip()
    if display_info:
        title = display_info.splitlines()[0].strip()
        title = strip_markup(title)
        if title:
            return title
    meta_data = item.get("meta_data") if isinstance(item.get("meta_data"), dict) else {}
    for key in ("title", "summary", "topic"):
        value = str(meta_data.get(key) or "").strip()
        if value:
            return strip_markup(value)
    return fallback


def meeting_item_in_range(item: dict[str, Any], start_dt: datetime, end_dt: datetime) -> bool:
    meeting_dt = parse_meeting_datetime_from_display(str(item.get("display_info") or ""), start_dt, end_dt)
    if not meeting_dt:
        return True
    return start_dt <= meeting_dt < end_dt


def parse_meeting_datetime_from_display(value: str, start_dt: datetime, end_dt: datetime) -> datetime | None:
    normalized = strip_markup(value)
    full_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})", normalized)
    if full_match:
        year, month, day, hour, minute = map(int, full_match.groups())
        return datetime(year, month, day, hour, minute, tzinfo=start_dt.tzinfo)

    short_match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})", normalized)
    if not short_match:
        return None

    month, day, hour, minute = map(int, short_match.groups())
    years = list(dict.fromkeys([start_dt.year, end_dt.year, datetime.now().astimezone().year]))
    for year in years:
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=start_dt.tzinfo)
        except ValueError:
            continue
        if start_dt <= candidate < end_dt:
            return candidate
    try:
        return datetime(start_dt.year, month, day, hour, minute, tzinfo=start_dt.tzinfo)
    except ValueError:
        return None


def strip_markup(value: str) -> str:
    return re.sub(r"<[^>]+>|＜[^＞]+＞", "", value).strip()


def extract_minute_token(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if part == "minutes" and index + 1 < len(parts):
            return parts[index + 1]
    match = re.search(r"/minutes/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


def write_recording_manifest(
    manifest: Any,
    status: str,
    meeting_id: str,
    title: str,
    minute_token: str | None,
    recording_url: str | None,
    output_path: Path | None,
    reason: str | None = None,
) -> None:
    manifest.write(
        json.dumps(
            {
                "status": status,
                "meeting_id": meeting_id,
                "title": title,
                "minute_token": minute_token,
                "recording_url": recording_url,
                "path": str(output_path) if output_path else None,
                "reason": reason,
                "time": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    manifest.flush()


def download_minutes_media(
    client: FeishuClient,
    args: argparse.Namespace,
    manifest: Any,
    meeting_id: str | None,
    title: str,
    minute_token: str,
    recording_url: str | None,
    output_path: Path,
) -> DownloadStats:
    if args.skip_existing and output_path.exists():
        print(f"skip existing recording: {title} -> {output_path}")
        write_recording_manifest(manifest, "skipped", meeting_id or "", title, minute_token, recording_url, output_path, "exists")
        return DownloadStats(skipped=1)

    if args.dry_run:
        print(f"download recording: {title} ({meeting_id or minute_token}) -> {output_path}")
        write_recording_manifest(manifest, "planned", meeting_id or "", title, minute_token, recording_url, output_path)
        return DownloadStats(planned=1)

    download_url = client.get_minutes_media_download_url(minute_token)
    client.download_external_url(download_url, output_path)
    print(f"download recording: {title} ({meeting_id or minute_token}) -> {output_path}")
    write_recording_manifest(manifest, "downloaded", meeting_id or "", title, minute_token, recording_url, output_path)
    return DownloadStats(downloaded=1)


def ensure_calendar_suffix(path: Path, export_format: str) -> Path:
    suffix = f".{export_format}"
    if path.suffix.lower() == suffix:
        return path
    return path.with_suffix(suffix)


def write_calendar_export(
    output_path: Path,
    export_format: str,
    events: list[dict[str, Any]],
    calendar: dict[str, Any],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if export_format == "json":
        payload = {"calendar": calendar, "events": events}
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if export_format == "csv":
        write_calendar_csv(output_path, events)
        return
    write_calendar_ics(output_path, events, calendar)


def write_calendar_csv(output_path: Path, events: list[dict[str, Any]]) -> None:
    fields = [
        "event_id",
        "summary",
        "start",
        "end",
        "location",
        "description",
        "visibility",
        "status",
        "app_link",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for event in events:
            writer.writerow(
                {
                    "event_id": event.get("event_id"),
                    "summary": event.get("summary"),
                    "start": format_event_time(event.get("start_time")),
                    "end": format_event_time(event.get("end_time")),
                    "location": extract_location(event.get("location")),
                    "description": event.get("description"),
                    "visibility": event.get("visibility"),
                    "status": event.get("status"),
                    "app_link": event.get("app_link"),
                }
            )


def write_calendar_ics(output_path: Path, events: list[dict[str, Any]], calendar: dict[str, Any]) -> None:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//feishu-doc-down//calendar-export//CN",
        f"X-WR-CALNAME:{ics_escape(calendar.get('summary') or 'Feishu Calendar')}",
    ]
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for event in events:
        uid = event.get("event_id") or event.get("uid") or secrets.token_hex(8)
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{ics_escape(str(uid))}",
                f"DTSTAMP:{now}",
                f"SUMMARY:{ics_escape(str(event.get('summary') or ''))}",
            ]
        )
        start = event_time_to_ics(event.get("start_time"))
        end = event_time_to_ics(event.get("end_time"))
        if start:
            lines.append(start.replace("DT", "DTSTART", 1))
        if end:
            lines.append(end.replace("DT", "DTEND", 1))
        location = extract_location(event.get("location"))
        if location:
            lines.append(f"LOCATION:{ics_escape(location)}")
        description = event.get("description")
        if description:
            lines.append(f"DESCRIPTION:{ics_escape(str(description))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    output_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def format_event_time(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("date"):
        return str(value["date"])
    timestamp = value.get("timestamp")
    if timestamp:
        return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()
    return ""


def event_time_to_ics(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    if value.get("date"):
        return f"DT;VALUE=DATE:{str(value['date']).replace('-', '')}"
    timestamp = value.get("timestamp")
    if timestamp:
        return "DT:" + datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return None


def extract_location(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name") or value.get("address") or "")
    return ""


def ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def auth_status_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feishu-doc-down auth status",
        description="Show saved auth state without printing secrets.",
    )
    parser.add_argument("--config", type=Path, default=default_config_path(), help="saved auth config path")
    args = parser.parse_args(argv)

    config = load_token_config(args.config)
    if not config:
        print(f"not authorized: no config at {args.config}")
        return 1

    token = get_saved_access_token(config)
    expires_at = int(config.get("expires_at") or 0)
    print(f"config: {args.config}")
    print(f"app_id: {config.get('app_id') or '(missing)'}")
    print(f"base_url: {config.get('base_url') or '(missing)'}")
    print(f"scope: {config.get('scope') or '(missing)'}")
    print(f"access_token: {'present' if token else 'missing'}")
    print(f"refresh_token: {'present' if config.get('refresh_token') else 'missing'}")
    if expires_at:
        remaining = expires_at - int(time.time())
        print(f"expires_in: {remaining} seconds")
    return 0 if token else 1


def auth_whoami_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feishu-doc-down auth whoami",
        description="Show the Feishu user bound to the saved token.",
    )
    parser.add_argument("--config", type=Path, default=default_config_path(), help="saved auth config path")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP request timeout in seconds")
    args = parser.parse_args(argv)

    try:
        config = load_token_config(args.config)
        token = get_saved_access_token(config)
        if not token:
            raise FeishuError("missing token; run `feishu-doc-down auth`")
        client = FeishuClient(token, config.get("base_url") or "https://open.feishu.cn", args.timeout)
        data = client._request_json("GET", "/open-apis/authen/v1/user_info")
    except (FeishuError, OSError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for key in ("name", "en_name", "tenant_key", "open_id", "union_id"):
        if data.get(key):
            print(f"{key}: {data[key]}")
    return 0


def run_oauth_flow(args: argparse.Namespace) -> dict[str, Any]:
    redirect = urlparse(args.redirect_uri)
    if redirect.scheme != "http" or redirect.hostname not in {"127.0.0.1", "localhost"}:
        raise FeishuError("this CLI auth flow only supports local http redirect URIs")
    if not redirect.port:
        raise FeishuError("redirect URI must include a port, for example http://127.0.0.1:8765/callback")

    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = pkce_challenge(code_verifier)
    auth_url = build_authorization_url(args, state, code_challenge)

    callback = wait_for_oauth_callback(auth_url, redirect, state, args.timeout, args.no_browser, args.no_qr)
    code = callback.get("code")
    if not code:
        raise FeishuError(f"authorization did not return code: {callback}")
    return exchange_authorization_code(args, code, code_verifier)


def wait_for_oauth_callback(
    auth_url: str,
    redirect: Any,
    expected_state: str,
    timeout: int,
    no_browser: bool,
    no_qr: bool,
) -> dict[str, str]:
    result: dict[str, str] = {}
    expected_path = redirect.path or "/"

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}

            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            result.update(query)
            if query.get("state") != expected_state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"OAuth state mismatch. You can close this tab.")
                return

            if query.get("error"):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Authorization failed. You can close this tab.")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("授权完成，可以关闭这个页面。".encode("utf-8"))

        def log_message(self, _format: str, *args: Any) -> None:
            return

    server = HTTPServer((redirect.hostname, redirect.port), CallbackHandler)
    server.timeout = 1

    print("open this URL or scan the QR code to authorize:")
    print(auth_url)
    if not no_qr:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(auth_url)
        qr.make(fit=True)
        qr.print_ascii(tty=True)

    if not no_browser:
        try:
            import webbrowser

            webbrowser.open(auth_url)
        except Exception:
            pass

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not result:
            server.handle_request()
    finally:
        server.server_close()

    if not result:
        raise FeishuError("timed out waiting for OAuth callback")
    if result.get("state") != expected_state:
        raise FeishuError("OAuth state mismatch")
    if result.get("error"):
        raise FeishuError(result.get("error_description") or result["error"])
    return result


def build_authorization_url(args: argparse.Namespace, state: str, code_challenge: str) -> str:
    query = {
        "client_id": args.app_id,
        "response_type": "code",
        "redirect_uri": args.redirect_uri,
        "scope": args.scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{args.auth_url}?{urlencode(query)}"


def exchange_authorization_code(args: argparse.Namespace, code: str, code_verifier: str) -> dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "client_id": args.app_id,
        "client_secret": args.app_secret,
        "code": code,
        "redirect_uri": args.redirect_uri,
        "code_verifier": code_verifier,
    }
    return oauth_token_request(args.base_url, payload, args.timeout)


def refresh_access_token(config: dict[str, Any], timeout: int) -> dict[str, Any]:
    refresh_token = config.get("refresh_token")
    if not refresh_token:
        raise FeishuError("saved token expired and no refresh_token is available; run `feishu-doc-down auth`")

    payload = {
        "grant_type": "refresh_token",
        "client_id": config.get("app_id"),
        "client_secret": config.get("app_secret"),
        "refresh_token": refresh_token,
    }
    if not payload["client_id"] or not payload["client_secret"]:
        raise FeishuError("saved token expired and app credentials are missing; run `feishu-doc-down auth`")
    if config.get("scope"):
        payload["scope"] = config["scope"]
    return oauth_token_request(config.get("base_url") or "https://open.feishu.cn", payload, timeout)


def oauth_token_request(base_url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    response = requests.post(
        f"{base_url.rstrip('/')}/open-apis/authen/v2/oauth/token",
        headers={"Content-Type": "application/json; charset=utf-8"},
        json=payload,
        timeout=timeout,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise FeishuError(f"OAuth token endpoint returned non-JSON response: HTTP {response.status_code}") from exc

    if response.status_code >= 400 or body.get("code") not in {0, None}:
        message = body.get("error_description") or body.get("msg") or body.get("error") or body
        raise FeishuError(f"OAuth token request failed: HTTP {response.status_code} {message}")

    token_data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not get_token_from_response(token_data):
        raise FeishuError("OAuth token response does not contain access_token/user_access_token")
    return token_data


def resolve_access_token(args: argparse.Namespace) -> str:
    direct_token = getattr(args, "token", None)
    if direct_token:
        return direct_token

    config = load_token_config(args.config)
    access_token = get_saved_access_token(config)
    if not access_token:
        raise FeishuError("missing token; run `feishu-doc-down auth` or pass --token")

    expires_at = float(config.get("expires_at") or 0)
    if expires_at and time.time() >= expires_at - TOKEN_EXPIRY_SKEW_SECONDS:
        token_data = refresh_access_token(config, args.timeout)
        config.update(token_data)
        config["expires_at"] = int(time.time() + int(token_data.get("expires_in") or 0))
        refresh_expires_in = token_data.get("refresh_token_expires_in") or token_data.get("refresh_expires_in")
        if refresh_expires_in:
            config["refresh_token_expires_at"] = int(time.time() + int(refresh_expires_in))
        save_config_dict(args.config, config)
        access_token = get_token_from_response(token_data)

    return str(access_token)


def load_token_config(config_path: Path) -> dict[str, Any]:
    return load_json_config(config_path)


def load_app_config(config_path: Path) -> dict[str, Any]:
    return load_json_config(config_path)


def load_json_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_first_json_config(paths: Iterable[Path]) -> dict[str, Any]:
    for path in paths:
        config = load_json_config(path)
        if config:
            return config
    return {}


def app_config_candidates(explicit_path: Path | None = None) -> list[Path]:
    if explicit_path:
        return [explicit_path]
    return [project_app_config_path(), bundled_app_config_path(), default_app_config_path()]


def apply_saved_app_credentials(args: argparse.Namespace) -> None:
    config = load_first_json_config(app_config_candidates(args.app_config))
    if not args.app_id:
        args.app_id = config.get("app_id")
    if not args.app_secret:
        args.app_secret = config.get("app_secret")
    if getattr(args, "base_url", None) == "https://open.feishu.cn" and config.get("base_url"):
        args.base_url = config["base_url"]
    if getattr(args, "auth_url", None) == "https://accounts.feishu.cn/open-apis/authen/v1/authorize" and config.get("auth_url"):
        args.auth_url = config["auth_url"]
    if getattr(args, "redirect_uri", None) == "http://127.0.0.1:8765/callback" and config.get("redirect_uri"):
        args.redirect_uri = config["redirect_uri"]
    if getattr(args, "scope", None) == DEFAULT_SCOPE and config.get("scope"):
        args.scope = config["scope"]


def save_token_config(config_path: Path, args: argparse.Namespace, token_data: dict[str, Any]) -> None:
    now = int(time.time())
    config = {
        "app_id": args.app_id,
        "app_secret": args.app_secret,
        "base_url": args.base_url,
        "auth_url": args.auth_url,
        "redirect_uri": args.redirect_uri,
        "scope": token_data.get("scope") or args.scope,
        "access_token": get_token_from_response(token_data),
        "refresh_token": token_data.get("refresh_token"),
        "token_type": token_data.get("token_type"),
        "expires_at": now + int(token_data.get("expires_in") or 0),
        "created_at": now,
    }
    refresh_expires_in = token_data.get("refresh_token_expires_in") or token_data.get("refresh_expires_in")
    if refresh_expires_in:
        config["refresh_token_expires_at"] = now + int(refresh_expires_in)
    save_config_dict(config_path, config)


def get_token_from_response(token_data: dict[str, Any]) -> str | None:
    return token_data.get("access_token") or token_data.get("user_access_token")


def get_saved_access_token(config: dict[str, Any]) -> str | None:
    return config.get("access_token") or config.get("user_access_token")


def save_config_dict(config_path: Path, config: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(config_path, 0o600)


def default_config_path() -> Path:
    if os.name == "nt" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / "feishu-doc-down" / "token.json"
    config_home = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "feishu-doc-down" / "token.json"


def default_app_config_path() -> Path:
    if os.name == "nt" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / "feishu-doc-down" / "app.json"
    config_home = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "feishu-doc-down" / "app.json"


def project_app_config_path() -> Path:
    return Path.cwd() / ".feishu-doc-down" / "app.json"


def bundled_app_config_path() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / ".feishu-doc-down" / "app.json"
    return Path("__no_bundled_app_config__")


def pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def walk_folder(
    client: FeishuClient,
    args: argparse.Namespace,
    manifest: Any,
    folder_token: str,
    local_dir: Path,
    remote_path: list[str],
    visited_folders: set[str],
) -> DownloadStats:
    stats = DownloadStats()
    local_dir.mkdir(parents=True, exist_ok=True)
    if folder_token in visited_folders:
        print(f"skip visited folder: {'/'.join(remote_path) or folder_token}")
        return stats.add(skipped=1)
    visited_folders.add(folder_token)

    items = list(client.list_files(folder_token, args.page_size))
    folder_name_counts = count_folder_names(items)

    for item in items:
        item_type = str(item.get("type") or "")
        item_name = str(item.get("name") or item.get("token") or "untitled")
        token = str(item.get("token") or "")

        if item_type == "shortcut":
            current_remote_path = remote_path + [item_name]
            if not args.include_shortcuts:
                print(f"skip shortcut: {'/'.join(current_remote_path)}")
                stats = stats.add(skipped=1)
                write_manifest(manifest, "skipped", item, current_remote_path, None, reason="shortcut")
                continue
            shortcut_info = item.get("shortcut_info") or {}
            token = shortcut_info.get("target_token") or token
            item_type = shortcut_info.get("target_type") or item_type

        safe_name = sanitize_filename(item_name)
        current_remote_path = remote_path + [item_name]

        if item_type in FOLDER_TYPES:
            next_dir = local_dir / disambiguate_folder_name(safe_name, token, folder_name_counts)
            print(f"folder: {'/'.join(current_remote_path)}")
            if not args.dry_run:
                next_dir.mkdir(parents=True, exist_ok=True)
            stats = merge_stats(
                stats,
                walk_folder(client, args, manifest, token, next_dir, current_remote_path, visited_folders),
            )
            continue

        try:
            result = handle_file(client, args, item, item_type, token, local_dir, safe_name, current_remote_path)
        except (requests.RequestException, FeishuError, OSError) as exc:
            print(f"failed: {'/'.join(current_remote_path)} ({exc})", file=sys.stderr)
            stats = stats.add(failed=1)
            write_manifest(manifest, "failed", item, current_remote_path, None, reason=str(exc))
            continue

        stats = stats.add(**{result["stat"]: 1})
        write_manifest(manifest, result["status"], item, current_remote_path, result.get("path"), reason=result.get("reason"))

    return stats


def walk_urls(
    client: FeishuClient,
    args: argparse.Namespace,
    manifest: Any,
    local_dir: Path,
) -> DownloadStats:
    stats = DownloadStats()
    local_dir.mkdir(parents=True, exist_ok=True)

    for url in args.url:
        try:
            item = item_from_url(url)
            result = handle_file(
                client,
                args,
                item,
                str(item["type"]),
                str(item["token"]),
                local_dir,
                sanitize_filename(str(item["name"])),
                [str(item["name"])],
            )
        except (ValueError, requests.RequestException, FeishuError, OSError) as exc:
            print(f"failed: {url} ({exc})", file=sys.stderr)
            stats = stats.add(failed=1)
            write_manifest(manifest, "failed", {"url": url}, [url], None, reason=str(exc))
            continue

        stats = stats.add(**{result["stat"]: 1})
        write_manifest(manifest, result["status"], item, [str(item["name"])], result.get("path"), reason=result.get("reason"))

    return stats


def item_from_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("unsupported Feishu document URL")

    prefix = parts[0]
    token = parts[1]
    item_type = URL_TYPE_BY_PATH_PREFIX.get(prefix)
    if not item_type:
        raise ValueError(f"unsupported Feishu document URL type: /{prefix}/")

    title = token
    query = parse_qs(parsed.query)
    if query.get("title"):
        title = query["title"][0]
    return {"type": item_type, "token": token, "name": title, "url": url}


def walk_explorer_folder(
    client: FeishuClient,
    args: argparse.Namespace,
    manifest: Any,
    folder_token: str,
    local_dir: Path,
    remote_path: list[str],
    visited_folders: set[str],
) -> DownloadStats:
    stats = DownloadStats()
    local_dir.mkdir(parents=True, exist_ok=True)
    if folder_token in visited_folders:
        print(f"skip visited folder: {'/'.join(remote_path) or folder_token}")
        return stats.add(skipped=1)
    visited_folders.add(folder_token)

    items = list(client.list_explorer_children(folder_token))
    folder_name_counts = count_folder_names(items)

    for item in items:
        item_type = str(item.get("type") or "")
        item_name = str(item.get("name") or item.get("token") or "untitled")
        token = str(item.get("token") or "")
        safe_name = sanitize_filename(item_name)
        current_remote_path = remote_path + [item_name]

        if item.get("is_shortcut") and (not token or item_type == "shortcut"):
            print(f"skip unresolved shortcut: {'/'.join(current_remote_path)}")
            stats = stats.add(skipped=1)
            write_manifest(manifest, "skipped", item, current_remote_path, None, reason="unresolved shortcut")
            continue

        if item_type in FOLDER_TYPES:
            next_dir = local_dir / disambiguate_folder_name(safe_name, token, folder_name_counts)
            print(f"folder: {'/'.join(current_remote_path)}")
            if not args.dry_run:
                next_dir.mkdir(parents=True, exist_ok=True)
            stats = merge_stats(
                stats,
                walk_explorer_folder(client, args, manifest, token, next_dir, current_remote_path, visited_folders),
            )
            continue

        try:
            result = handle_file(client, args, item, item_type, token, local_dir, safe_name, current_remote_path)
        except (requests.RequestException, FeishuError, OSError) as exc:
            print(f"failed: {'/'.join(current_remote_path)} ({exc})", file=sys.stderr)
            stats = stats.add(failed=1)
            write_manifest(manifest, "failed", item, current_remote_path, None, reason=str(exc))
            continue

        stats = stats.add(**{result["stat"]: 1})
        write_manifest(manifest, result["status"], item, current_remote_path, result.get("path"), reason=result.get("reason"))

    return stats


def walk_search(
    client: FeishuClient,
    args: argparse.Namespace,
    manifest: Any,
    local_dir: Path,
) -> DownloadStats:
    stats = DownloadStats()
    local_dir.mkdir(parents=True, exist_ok=True)

    for item in client.search_docs(args.search_key, args.search_count):
        item_type = str(item.get("type") or "")
        item_name = str(item.get("name") or item.get("token") or "untitled")
        token = str(item.get("token") or "")
        remote_path = [item_name]

        try:
            result = handle_file(
                client,
                args,
                item,
                item_type,
                token,
                local_dir,
                sanitize_filename(item_name),
                remote_path,
            )
        except (requests.RequestException, FeishuError, OSError) as exc:
            print(f"failed: {item_name} ({exc})", file=sys.stderr)
            stats = stats.add(failed=1)
            write_manifest(manifest, "failed", item, remote_path, None, reason=str(exc))
            continue

        stats = stats.add(**{result["stat"]: 1})
        write_manifest(manifest, result["status"], item, remote_path, result.get("path"), reason=result.get("reason"))

    return stats


def handle_file(
    client: FeishuClient,
    args: argparse.Namespace,
    item: dict[str, Any],
    item_type: str,
    token: str,
    local_dir: Path,
    safe_name: str,
    remote_path: list[str],
) -> dict[str, Any]:
    if item_type in ONLINE_TYPES:
        extension = export_extension(item_type, args)
        output_path = ensure_suffix(local_dir / safe_name, extension)
        if output_path.exists():
            if args.skip_existing:
                print(f"skip existing: {output_path}")
                return {"status": "skipped", "stat": "skipped", "path": str(output_path), "reason": "exists"}
            output_path = unique_path(output_path)
        print(f"export {item_type}: {'/'.join(remote_path)} -> {output_path}")
        if args.dry_run:
            return {"status": "dry-run", "stat": "planned", "path": str(output_path)}

        ticket = client.create_export_task(token, item_type, extension)
        result = client.wait_export_task(ticket, token, args.poll_interval, args.export_timeout)
        client.download_exported_file(result["file_token"], output_path)
        return {"status": "exported", "stat": "exported", "path": str(output_path)}

    if item_type in REGULAR_FILE_TYPES:
        output_path = local_dir / safe_name
        if output_path.exists():
            if args.skip_existing:
                print(f"skip existing: {output_path}")
                return {"status": "skipped", "stat": "skipped", "path": str(output_path), "reason": "exists"}
            output_path = unique_path(output_path)
        print(f"download file: {'/'.join(remote_path)} -> {output_path}")
        if args.dry_run:
            return {"status": "dry-run", "stat": "planned", "path": str(output_path)}
        client.download_regular_file(token, output_path)
        return {"status": "downloaded", "stat": "downloaded", "path": str(output_path)}

    if item_type in SKIP_TYPES:
        reason = f"unsupported online type: {item_type}"
    else:
        reason = f"unknown type: {item_type}"
    print(f"skip {item_type}: {'/'.join(remote_path)}")
    return {"status": "skipped", "stat": "skipped", "reason": reason}


def export_extension(item_type: str, args: argparse.Namespace) -> str:
    if item_type in {"doc", "docx"}:
        return args.doc_format
    if item_type == "sheet":
        return args.sheet_format
    if item_type == "bitable":
        return args.bitable_format
    raise FeishuError(f"unsupported export type: {item_type}")


def sanitize_filename(name: str) -> str:
    clean = INVALID_FILENAME_CHARS.sub("_", name).strip().strip(".")
    return clean or "untitled"


def count_folder_names(items: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        item_type = str(item.get("type") or "")
        if item_type not in FOLDER_TYPES:
            continue
        name = sanitize_filename(str(item.get("name") or item.get("token") or "untitled"))
        counts[name] = counts.get(name, 0) + 1
    return counts


def disambiguate_folder_name(safe_name: str, token: str, folder_name_counts: dict[str, int]) -> str:
    if folder_name_counts.get(safe_name, 0) <= 1:
        return safe_name
    token_suffix = sanitize_filename(token)[-8:] if token else secrets.token_hex(4)
    return f"{safe_name}-{token_suffix}"


def ensure_suffix(path: Path, extension: str) -> Path:
    suffix = f".{extension}"
    if path.name.lower().endswith(suffix.lower()):
        return path
    return path.with_name(path.name + suffix)


def unique_path(path: Path, is_dir: bool = False) -> Path:
    if not path.exists():
        return path

    stem = path.name if is_dir else path.stem
    suffix = "" if is_dir else path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"could not find available path for {path}")


def merge_stats(left: DownloadStats, right: DownloadStats) -> DownloadStats:
    return DownloadStats(
        exported=left.exported + right.exported,
        downloaded=left.downloaded + right.downloaded,
        planned=left.planned + right.planned,
        skipped=left.skipped + right.skipped,
        failed=left.failed + right.failed,
    )


def write_manifest(
    manifest: Any,
    status: str,
    item: dict[str, Any],
    remote_path: list[str],
    local_path: str | None,
    reason: str | None = None,
) -> None:
    record = {
        "status": status,
        "remote_path": remote_path,
        "local_path": local_path,
        "type": item.get("type"),
        "token": item.get("token"),
        "url": item.get("url"),
    }
    if reason:
        record["reason"] = reason
    manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
    manifest.flush()


if __name__ == "__main__":
    raise SystemExit(main())
