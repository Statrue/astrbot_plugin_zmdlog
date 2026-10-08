"""Deploy to, reload and smoke-test the plugin on a running AstrBot.

Development tool only (not used at runtime). It talks to AstrBot's OpenAPI
(4.28+) with an API key created under WebUI 设置 → OpenAPI. The scopes it uses
are ``plugin`` (reload), ``chat`` (smoke) and ``file`` (download the replies'
pictures; without it the smoke still runs and reports no sizes). The logs
endpoints want ``system``, which no API key can hold, so a Traceback is still
read with ``docker logs``.

    python tools/server.py smoke [--only 榜单 战报 ...] [--save DIR]
    python tools/server.py reload
    python tools/server.py deploy      # git pull in the container, reload, smoke

Settings come from a JSON file (``--config``, default ``docs/debug/server.json``,
gitignored) — ``base_url``, ``key_file`` (relative to the config file), and for
``deploy`` also ``ssh``, ``container`` and ``plugin_dir``; ``smoke_board``,
``smoke_character`` and the optional ``smoke_account`` name what the smoke
queries. The key is read from ``key_file`` and never printed.

The smoke sends each command through WebChat, which AstrBot treats as a
private chat, as a user who is not an AstrBot admin, and checks the reply's
kind — a picture or a text — against what that command answers. It runs the
commands one at a time, so each time is that command's own fetch and render,
and it never sends 关注, 绑定 or 别名: those write state a WebChat session
would keep.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "docs" / "debug" / "server.json"
PLUGIN_ID = "astrbot_plugin_zmdlog"
SMOKE_USER = "zmdlog-smoke"
REPLY_TIMEOUT = 120.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Case:
    label: str
    message: str
    expect: str  # "image" or "plain"


def smoke_cases(config: dict) -> list[Case]:
    board = config.get("smoke_board") or "罗丹"
    character = config.get("smoke_character") or "莱万汀"
    cases = [
        Case("帮助", "zmdlog", "image"),
        Case("帮助别名", "zmdlog 帮助", "image"),
        Case("榜单列表", "zmdlog 榜单", "plain"),
        Case("榜单", f"zmdlog {board}", "image"),
        Case("榜单第2页", f"zmdlog {board} --页 2", "image"),
        Case("榜单全部", f"zmdlog {board} --页 全部", "image"),
        Case("阵容", f"zmdlog 阵容 {board}", "image"),
        Case("战报", f"zmdlog 战报 {board} 1", "image"),
        Case("数据", f"zmdlog 数据 {board} 1", "image"),
        Case("排轴", f"zmdlog 排轴 {board} 1", "image"),
        Case("养成", f"zmdlog 养成 {board} 1", "image"),
        Case("对比", f"zmdlog 对比 {board} 1 2", "image"),
        Case("新纪录", "zmdlog 新纪录", "image"),
        Case("玩家排名", "zmdlog 玩家排名", "image"),
        Case("角色排名", "zmdlog 角色排名", "image"),
        Case("角色统计", f"zmdlog 角色统计 {character}", "image"),
        Case("角色档案", f"zmdlog 角色档案 {character}", "image"),
    ]
    if account := config.get("smoke_account"):
        cases += [
            Case("账号", f"zmdlog 账号 {account}", "image"),
            Case("趋势", f"zmdlog 趋势 {account}", "image"),
        ]
    return cases


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    key_path = path.resolve().parent / config["key_file"]
    config["_key"] = key_path.read_text(encoding="utf-8").strip()
    return config


def api(config: dict) -> httpx.Client:
    return httpx.Client(
        base_url=config["base_url"].rstrip("/") + "/api/v1",
        headers={"X-API-Key": config["_key"]},
        timeout=REPLY_TIMEOUT,
    )


def reload_plugin(config: dict) -> bool:
    with api(config) as client:
        reply = client.post("/plugins/reload", json={"plugin_id": PLUGIN_ID}).json()
        if reply.get("status") != "ok":
            print(f"reload failed: {reply.get('message')}")
            return False
        plugins = client.get("/plugins").json().get("data") or []
    version = next(
        (p.get("version") for p in plugins if p.get("name") == PLUGIN_ID), "?"
    )
    print(f"reloaded {PLUGIN_ID} {version}")
    return True


def ask(client: httpx.Client, session_id: str, message: str) -> list[dict]:
    """Send one message and collect the bot's reply parts until ``end``.

    A picture part carries the attachment id its ``attachment_saved`` event
    names, which is what ``/file`` downloads it by.
    """
    body = {
        "message": message,
        "username": SMOKE_USER,
        "session_id": session_id,
        "enable_streaming": False,
    }
    parts: list[dict] = []
    with client.stream("POST", "/chat", json=body) as response:
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line.removeprefix("data: "))
            kind = event.get("type")
            if kind == "end":
                break
            if kind == "error":
                parts.append({"type": "error", "data": event.get("data")})
                break
            if kind in ("plain", "image"):
                parts.append(event)
            elif kind == "attachment_saved" and parts:
                parts[-1]["attachment_id"] = (event.get("data") or {}).get("id")
    return parts


def fetch_png(client: httpx.Client, attachment_id: str) -> bytes | None:
    response = client.get("/file", params={"attachment_id": attachment_id})
    if not response.content.startswith(PNG_SIGNATURE):
        return None  # no `file` scope, or the attachment is gone
    return response.content


def png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


def describe_image(
    client: httpx.Client, part: dict, save_to: Path | None, label: str
) -> str:
    attachment_id = part.get("attachment_id")
    data = fetch_png(client, attachment_id) if attachment_id else None
    if data is None:
        return "png ?"
    width, height = png_size(data)
    if save_to is not None:
        save_to.mkdir(parents=True, exist_ok=True)
        (save_to / f"{label}.png").write_bytes(data)
    return f"{width}×{height} {len(data) / 1024:.0f} KB"


def smoke(config: dict, only: list[str] | None, save_to: Path | None) -> bool:
    cases = [c for c in smoke_cases(config) if not only or c.label in only]
    session_id = f"{SMOKE_USER}-{int(time.time())}"
    failures = 0
    with api(config) as client:
        for case in cases:
            started = time.monotonic()
            try:
                parts = ask(client, session_id, case.message)
            except httpx.HTTPError as exc:
                parts = [{"type": "error", "data": type(exc).__name__}]
            elapsed = time.monotonic() - started
            kinds = [p["type"] for p in parts]
            ok = case.expect in kinds and "error" not in kinds
            failures += not ok
            details = [
                describe_image(client, p, save_to, case.label)
                if p["type"] == "image"
                else str(p.get("data", "")).replace("\n", " ")[:70]
                for p in parts
            ]
            shown = ",".join(kinds) or "nothing"
            print(
                f"{'ok  ' if ok else 'FAIL'} {elapsed:5.1f}s  {case.label:<6} "
                f"[{shown}] {' / '.join(details)}"
            )
    print(f"{len(cases) - failures}/{len(cases)} as expected")
    return failures == 0


def pull(config: dict) -> bool:
    command = (
        f"docker exec {config['container']} git -C {config['plugin_dir']} "
        "pull --ff-only"
    )
    result = subprocess.run(["ssh", config["ssh"], command], check=False)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("smoke", "reload", "deploy"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--only", nargs="+", metavar="LABEL")
    parser.add_argument("--save", type=Path, metavar="DIR")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.action == "smoke":
        return 0 if smoke(config, args.only, args.save) else 1
    if args.action == "reload":
        return 0 if reload_plugin(config) else 1
    if not pull(config) or not reload_plugin(config):
        return 1
    return 0 if smoke(config, args.only, args.save) else 1


if __name__ == "__main__":
    sys.exit(main())
