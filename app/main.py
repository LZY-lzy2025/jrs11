import datetime as dt
import json
import os
import re
import asyncio
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, request


@dataclass
class Config:
    source_url: str
    play_link_host_filter: str
    play_host_prefix: str
    keywords_regex: str
    schedule_minutes: int
    tz_name: str
    output_file: Path
    ids_file: Path
    timeout_seconds: int
    capture_wait_ms: int
    host: str
    port: int


def load_config() -> Config:
    return Config(
        source_url=os.getenv("SOURCE_URL", "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5").strip(),
        play_link_host_filter=os.getenv("PLAY_LINK_HOST_FILTER", "play.sportsteam368.com").strip(),
        play_host_prefix=os.getenv("PLAY_HOST_PREFIX", "http://play.sportsteam368.com").strip(),
        keywords_regex=os.getenv("KEYWORDS_REGEX", r"高清直播|蓝光"),
        schedule_minutes=int(os.getenv("SCHEDULE_MINUTES", "25")),
        tz_name=os.getenv("TZ_NAME", "Asia/Shanghai"),
        output_file=Path(os.getenv("OUTPUT_FILE", "output/tokens.txt")),
        ids_file=Path(os.getenv("IDS_FILE", "output/ids.json")),
        timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        capture_wait_ms=int(os.getenv("CAPTURE_WAIT_MS", "6000")),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
    )


def now_in_tz(tz_name: str) -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(tz_name))
    except Exception:
        return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=8))
        )


def fetch_text(url: str, timeout_seconds: int) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=timeout_seconds)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def extract_document_write_lines(js_text: str) -> list[str]:
    return re.findall(r"document\.write\('([^']*)'\);", js_text)


def parse_mmdd_hhmm_to_datetime(value: str, now_bj: dt.datetime) -> dt.datetime | None:
    m = re.match(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2})$", value)
    if not m:
        return None
    month, day, hour, minute = map(int, m.groups())

    candidates = []
    for y in (now_bj.year - 1, now_bj.year, now_bj.year + 1):
        try:
            candidates.append(
                now_bj.replace(
                    year=y,
                    month=month,
                    day=day,
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )
            )
        except ValueError:
            pass

    if not candidates:
        return None

    return min(candidates, key=lambda d: abs((d - now_bj).total_seconds()))


def within_3h(event_time: dt.datetime, now_bj: dt.datetime) -> bool:
    return abs((event_time - now_bj).total_seconds()) <= 3 * 3600


def extract_match_items(js_text: str, league_prefix: str = "JRS") -> list[dict]:
    lines = extract_document_write_lines(js_text)
    items: list[dict] = []
    current: dict | None = None

    league_re = re.compile(r'class="lab_events"[^>]*><span class="name">([^<]+)</span>')
    time_re = re.compile(r'class="lab_time">([^<]+)<')
    home_re = re.compile(r'class="lab_team_home"><strong class="name">([^<]+)</strong>')
    away_re = re.compile(r'class="lab_team_away"><strong class="name">([^<]+)</strong>')
    href_re = re.compile(r'href="([^"]+)"')

    for line in lines:
        if line.startswith('<ul class="item play'):
            current = {"league": "", "time": "", "home": "", "away": "", "hrefs": []}
            continue

        if current is None:
            continue

        m = league_re.search(line)
        if m:
            current["league"] = f"{league_prefix}{m.group(1).strip()}"

        m = time_re.search(line)
        if m:
            current["time"] = m.group(1).strip()

        m = home_re.search(line)
        if m:
            current["home"] = m.group(1).strip()

        m = away_re.search(line)
        if m:
            current["away"] = m.group(1).strip()

        for hm in href_re.findall(line):
            if hm.startswith("http://") or hm.startswith("https://"):
                current["hrefs"].append(hm.strip())

        if line == "</ul>":
            if current["league"] and current["time"] and current["home"] and current["away"]:
                current["hrefs"] = sorted(set(current["hrefs"]))
                items.append(current)
            current = None

    return items


def extract_data_play_urls(page_text: str, cfg: Config) -> list[str]:
    pattern = re.compile(
        rf'<a[^>]*data-play="([^"]+)"[^>]*>\s*<em[^>]*></em>\s*<strong>([^<]*({cfg.keywords_regex})[^<]*)</strong>',
        re.IGNORECASE,
    )

    urls = []
    for m in pattern.finditer(page_text):
        data_play = m.group(1).strip()
        full_url = urljoin(cfg.play_host_prefix.rstrip("/") + "/", data_play.lstrip("/"))
        urls.append(full_url)

    return sorted(set(urls))


def extract_paps_ids_from_urls(urls: Iterable[str]) -> list[str]:
    ids: set[str] = set()
    seen_urls: set[str] = set()

    def walk_url(value: str, depth: int = 0) -> None:
        if depth > 2:
            return
        value = (value or "").strip()
        if not value or value in seen_urls:
            return
        seen_urls.add(value)

        try:
            parsed = urlparse(value)
        except Exception:
            return

        if "paps.html" in parsed.path:
            query = parse_qs(parsed.query)
            for item in query.get("id", []):
                token = item.strip()
                if token:
                    ids.add(token)

        nested_values = []
        query_map = parse_qs(parsed.query)
        for vals in query_map.values():
            nested_values.extend(vals)
        for frag in nested_values:
            decoded = unquote(frag).strip()
            if "http://" in decoded or "https://" in decoded:
                walk_url(decoded, depth + 1)

    for value in urls:
        walk_url(value, 0)
    return sorted(ids)


async def capture_resource_urls_with_browser(url: str, timeout_seconds: int, capture_wait_ms: int) -> list[str]:
    """
    使用 Puppeteer 网络响应拦截抓取资源 URL，避免依赖正则从 HTML/JS 文本里猜路径。
    """
    script_path = Path(__file__).with_name("capture_paths.js")
    if not script_path.exists():
        msg = f"puppeteer script missing: {script_path}"
        with STATE.lock:
            STATE.last_puppeteer_error = msg
        print(f"[warn] {msg}")
        return []

    env = os.environ.copy()
    env["TARGET_URL"] = url
    env["NAV_TIMEOUT_MS"] = str(max(timeout_seconds, 1) * 1000)
    env["CAPTURE_WAIT_MS"] = str(max(capture_wait_ms, 0))

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["node", str(script_path)],
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds * 2, 15),
            env=env,
            check=False,
        )
    except Exception as exc:
        msg = f"puppeteer runner unavailable: {exc}"
        with STATE.lock:
            STATE.last_puppeteer_error = msg
        print(f"[warn] {msg}")
        return []

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        with STATE.lock:
            STATE.last_puppeteer_error = err
        print(f"[warn] puppeteer capture failed: {err}")
        return []

    try:
        data = json.loads(proc.stdout.strip() or "[]")
        if isinstance(data, list):
            with STATE.lock:
                STATE.last_puppeteer_error = None
            return sorted({str(x).strip() for x in data if str(x).strip()})
    except Exception as exc:
        with STATE.lock:
            STATE.last_puppeteer_error = f"puppeteer output parse failed: {exc}"
        print(f"[warn] puppeteer output parse failed: {exc}")
    return []


def extract_tokens_with_resource_tree(base_url: str, cfg: Config) -> list[str]:
    """
    通过真实浏览器抓取网络响应 URL，
    仅从资源路径参数中提取 paps.html?id=... 后面的 id，
    不再依赖页面正文正则匹配。
    """
    tokens: set[str] = set()
    try:
        urls = asyncio.run(
            capture_resource_urls_with_browser(base_url, cfg.timeout_seconds, cfg.capture_wait_ms)
        )
        tokens.update(extract_paps_ids_from_urls(urls))
    except Exception as exc:
        print(f"[warn] browser resource capture failed: {base_url} err={exc}")

    return sorted(tokens)


def write_tokens(path: Path, tokens: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(tokens) + ("\n" if tokens else ""), encoding="utf-8")


def read_tokens(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def write_ids(path: Path, ids: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")


def read_ids(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        return []
    return []


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.last_count: int = 0
        self.last_puppeteer_error: str | None = None


STATE = AppState()


def run_once(cfg: Config) -> None:
    if not cfg.source_url:
        raise ValueError("SOURCE_URL is empty")
    if not cfg.play_host_prefix:
        raise ValueError("PLAY_HOST_PREFIX is empty")

    now_bj = now_in_tz(cfg.tz_name)
    js_text = fetch_text(cfg.source_url, cfg.timeout_seconds)

    raw_items = extract_match_items(js_text, league_prefix="JRS")

    # 过滤到北京时间前后3小时的比赛，并建立 match_link -> match_meta 映射
    match_links: list[tuple[str, dict]] = []
    for item in raw_items:
        evt = parse_mmdd_hhmm_to_datetime(item["time"], now_bj)
        if not evt or not within_3h(evt, now_bj):
            continue
        meta = {
            "league": item["league"],
            "time": item["time"],
            "home": item["home"],
            "away": item["away"],
        }
        for href in item["hrefs"]:
            if cfg.play_link_host_filter and cfg.play_link_host_filter not in href:
                continue
            match_links.append((href, meta))

    # 候选页面 -> data-play
    data_play_tasks: list[tuple[str, dict]] = []
    seen_pair = set()
    for href, meta in match_links:
        try:
            page = fetch_text(href, cfg.timeout_seconds)
            for dp in extract_data_play_urls(page, cfg):
                key = (dp, meta["league"], meta["time"], meta["home"], meta["away"])
                if key not in seen_pair:
                    seen_pair.add(key)
                    data_play_tasks.append((dp, meta))
        except Exception as exc:
            print(f"[warn] open candidate failed: {href} err={exc}")

    # data-play -> ids，并与比赛信息一一对应
    mapped_ids: list[dict] = []
    seen_mapped = set()
    token_only: set[str] = set()

    for dp_url, meta in data_play_tasks:
        try:
            tokens = extract_tokens_with_resource_tree(dp_url, cfg)

            for token in tokens:
                token_only.add(token)
                row = {
                    "id": token,
                    "league": meta["league"],
                    "time": meta["time"],
                    "home": meta["home"],
                    "away": meta["away"],
                }
                sk = (row["id"], row["league"], row["time"], row["home"], row["away"])
                if sk not in seen_mapped:
                    seen_mapped.add(sk)
                    mapped_ids.append(row)
        except Exception as exc:
            print(f"[warn] open data-play failed: {dp_url} err={exc}")

    mapped_ids.sort(key=lambda x: (x["time"], x["league"], x["home"], x["away"], x["id"]))
    write_ids(cfg.ids_file, mapped_ids)
    write_tokens(cfg.output_file, sorted(token_only))

    with STATE.lock:
        STATE.last_run_at = now_bj.isoformat()
        STATE.last_error = None
        STATE.last_count = len(mapped_ids)

    print(f"[info] mapped ids={len(mapped_ids)} -> {cfg.ids_file}")


def scheduler_loop(cfg: Config) -> None:
    while True:
        try:
            run_once(cfg)
        except Exception as exc:
            with STATE.lock:
                STATE.last_error = str(exc)
            print(f"[error] {exc}")

        sleep_seconds = max(cfg.schedule_minutes, 1) * 60
        print(f"[info] sleep {sleep_seconds}s")
        time.sleep(sleep_seconds)


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        with STATE.lock:
            payload = {
                "status": "running",
                "last_run_at": STATE.last_run_at,
                "last_error": STATE.last_error,
                "mapped_id_count": STATE.last_count,
                "last_puppeteer_error": STATE.last_puppeteer_error,
                "ids_file": str(cfg.ids_file),
                "tokens_file": str(cfg.output_file),
                "endpoints": ["/", "/healthz", "/ids", "/ids.txt", "/debug", "/run-once"],
            }
        return jsonify(payload)

    @app.get("/healthz")
    def healthz() -> Response:
        return jsonify({"ok": True})

    @app.get("/ids")
    def ids_json() -> Response:
        data = read_ids(cfg.ids_file)
        return jsonify({"count": len(data), "items": data})

    @app.get("/ids.txt")
    def ids_text() -> Response:
        data = read_ids(cfg.ids_file)
        lines = [f'{i["league"]}|{i["time"]}|{i["home"]} vs {i["away"]}|{i["id"]}' for i in data]
        return Response("\n".join(lines) + ("\n" if lines else ""), mimetype="text/plain; charset=utf-8")

    @app.get("/debug")
    def debug_info() -> Response:
        target_url = request.args.get("url", "").strip()
        if not target_url:
            target_url = cfg.play_host_prefix

        def run_cmd(cmd: list[str], timeout: int = 5) -> dict:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
                return {
                    "ok": proc.returncode == 0,
                    "code": proc.returncode,
                    "stdout": (proc.stdout or "").strip(),
                    "stderr": (proc.stderr or "").strip(),
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        node_check = run_cmd(["node", "-v"])
        npm_check = run_cmd(["npm", "list", "puppeteer", "--depth=0"], timeout=10)

        capture_urls = asyncio.run(
            capture_resource_urls_with_browser(target_url, min(cfg.timeout_seconds, 15), cfg.capture_wait_ms)
        )
        capture_ids = extract_paps_ids_from_urls(capture_urls)
        sample_size = min(max(int(request.args.get("sample", "10")), 1), 50)

        with STATE.lock:
            runtime = {
                "last_run_at": STATE.last_run_at,
                "last_error": STATE.last_error,
                "last_puppeteer_error": STATE.last_puppeteer_error,
                "mapped_id_count": STATE.last_count,
            }

        payload = {
            "runtime": runtime,
            "target_url": target_url,
            "script_path": str(Path(__file__).with_name("capture_paths.js")),
            "node_check": node_check,
            "npm_puppeteer_check": npm_check,
            "capture": {
                "resource_url_count": len(capture_urls),
                "id_count": len(capture_ids),
                "sample_resource_urls": capture_urls[:sample_size],
                "sample_ids": capture_ids[:sample_size],
            },
        }
        return jsonify(payload)

    @app.post("/run-once")
    def trigger_once() -> Response:
        threading.Thread(target=run_once, args=(cfg,), daemon=True).start()
        return jsonify({"queued": True})

    return app


def main() -> None:
    cfg = load_config()

    thread = threading.Thread(target=scheduler_loop, args=(cfg,), daemon=True)
    thread.start()

    app = create_app(cfg)
    app.run(host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
