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
from datetime import datetime, timezone
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
        help="my-library downloads the Docs sidebar library; explorer/folder use Drive; search downloads searchable docs",
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
    parser.add_argument("--app-config", type=Path, default=default_app_config_path(), help="saved app credential config path")
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


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
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
    if raw_args and raw_args[0] == "download":
        return download_main(raw_args[1:])
    return download_main(raw_args)


def menu_main(argv: list[str] | None = None) -> int:
    args = parse_menu_args(argv)

    print("Feishu Doc Down")
    print("1. 云盘")
    print("2. 我的文档库")
    print("3. 全部")
    print("4. 搜索")
    print("5. 按链接下载")
    print("6. 导出日历日程")
    choice = prompt_choice("请选择下载来源 [1-6]", {"1", "2", "3", "4", "5", "6"}, "3")

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
                stats = walk_explorer_folder(client, args, manifest, root_token, args.output_dir, [], set())
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
    parser.add_argument("--app-config", type=Path, default=default_app_config_path(), help="saved app credential config path")
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
        walk_explorer_folder(client, args, manifest, root_token, output_dir / "drive", [], set()),
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

    for node in client.list_wiki_nodes("my_library", parent_node_token, min(args.page_size, 50)):
        item = item_from_wiki_node(node)
        item_type = str(item.get("type") or "")
        item_name = str(item.get("name") or item.get("token") or "untitled")
        token = str(item.get("token") or "")
        safe_name = sanitize_filename(item_name)
        current_remote_path = remote_path + [item_name]

        node_token = str(node.get("node_token") or "")
        if item_type == "folder":
            next_dir = local_dir / safe_name
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


def apply_saved_app_credentials(args: argparse.Namespace) -> None:
    config = load_app_config(args.app_config)
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

    for item in client.list_files(folder_token, args.page_size):
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
            next_dir = local_dir / safe_name
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

    for item in client.list_explorer_children(folder_token):
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
            next_dir = local_dir / safe_name
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
