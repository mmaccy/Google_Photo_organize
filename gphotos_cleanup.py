#!/usr/bin/env python3
"""Google フォト 容量消費メディアのバックアップ & 削除ツール。

Google フォトのストレージ管理ページ(quotamanagement)には「アカウントの保存容量を
消費している写真・動画だけ」が表示される(Pixel 特典アップロードや 2021 年 6 月以前の
高画質アップロードなど、容量にカウントされないものは表示されない)。

このツールはそのページを Playwright で自動操作し、1 件ずつ
  1. ダウンロード (Shift+D)
  2. ローカル保存の成功を確認
  3. ゴミ箱へ移動 (#)
を繰り返す。ダウンロードが確認できなかったものは削除しない。

使い方:
  python gphotos_cleanup.py login                 # 初回のみ: ブラウザで Google にログイン
  python gphotos_cleanup.py scan                  # 対象の一覧を確認(削除もDLもしない)
  python gphotos_cleanup.py run --no-delete       # ダウンロードのみ(お試し推奨)
  python gphotos_cleanup.py run                   # ダウンロード + ゴミ箱へ移動
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    print(
        "Playwright がインストールされていません。\n"
        "  pip install -r requirements.txt\n"
        "  playwright install chromium\n"
        "を実行してください。",
        file=sys.stderr,
    )
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "profile"
DEFAULT_OUT_DIR = BASE_DIR / "downloads"

PHOTOS_URL = "https://photos.google.com/"
# 容量を消費しているメディアだけがサイズ順に並ぶ公式ページ。
QUOTA_URL = "https://photos.google.com/quotamanagement/large"

# ツールバーの「ゴミ箱に移動」ボタンの文言(日本語 / 英語 UI 両対応)
TRASH_CONFIRM_RE = re.compile(
    r"(ゴミ箱に移動|ごみ箱に移動|Move to trash|Move to bin)", re.IGNORECASE
)
# 確認ダイアログ「アイテムをゴミ箱に移動します」の確定ボタン。
# 実画面では「キャンセル / OK」の 2 ボタン(完全一致で照合し、キャンセルを誤クリックしない)
DIALOG_OK_RE = re.compile(
    r"^\s*(OK|ゴミ箱に移動|ごみ箱に移動|Move to trash|Move to bin)\s*$", re.IGNORECASE
)
# ダイアログに role が付かない場合の画面全体フォールバック用。
# 「ゴミ箱に移動」を含めるとツールバーのボタンを誤って拾うため、OK 完全一致のみ。
OK_ONLY_RE = re.compile(r"^\s*OK\s*$", re.IGNORECASE)
# 削除完了時に画面下部へ出るトースト表示
TRASH_DONE_RE = re.compile(
    r"(ゴミ箱に移動しました|ごみ箱に移動しました|Moved to trash|Moved to bin)"
)

# ブラウザ再起動などの自動復旧を挟むため、少し余裕を持たせる
MAX_CONSECUTIVE_FAILURES = 5


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def launch_context(p):
    """ログイン状態を保持する永続プロファイルでブラウザを起動する。"""
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,  # Google ログインはヘッドレスでは弾かれるため常に表示
        accept_downloads=True,
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )


def get_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def is_closed_error(e: Exception) -> bool:
    """ページ/ブラウザが閉じられた(または落ちた)ことを示すエラーか。"""
    return "closed" in str(e).lower()


def cleanup_stray_pages(ctx, main_page) -> None:
    """ダウンロード失敗時に残る 404 エラータブや空タブを閉じる。"""
    for pg in list(ctx.pages):
        if pg == main_page:
            continue
        try:
            url = pg.url
            if "googleusercontent" in url or url in ("about:blank", ""):
                pg.close()
        except PWError:
            pass


def in_viewer(page) -> bool:
    # ダウンロード失敗時は googleusercontent のエラーページに飛ばされることが
    # あるため、Google フォトのビューア URL であることを厳密に確認する。
    return "photos.google.com" in page.url and "photo/" in page.url


# ストレージ管理ページのメディア行:
#   <div data-media-key="AF1Qip..."> の中に「選択」チェックボックスと「開く」ボタン、
#   テキストとして「(動画は再生時間) 日付 サイズ」を持つリスト形式。
# data-media-key はサイドバーのアルバム要素にも付くため、チェックボックスの有無で絞り込む。
ROW_SELECTOR = 'div[data-media-key]:has([role="checkbox"])'
OPEN_BUTTON_RE = re.compile(r"^(開く|Open)$")

# 旧レイアウト(グリッド+リンク型タイル)へのフォールバック
ANCHOR_SELECTORS = [
    'a[href*="quotamanagement"][href*="photo/"]',
    'a[href*="photo/"]',
]

SIZE_RE = re.compile(r"([\d.,]+)\s*(KB|MB|GB)", re.IGNORECASE)
DATE_RE = re.compile(r"\d{4}/\d{1,2}/\d{1,2}")


def find_tiles(page):
    """メディアの一覧要素を探す。(種類, locator) を返す。見つからなければ (None, None)。

    種類は 'row'(ストレージ管理ページのリスト行)または
    'anchor'(クリックでビューアが開くリンク型タイル)。
    """
    loc = page.locator(ROW_SELECTOR)
    if loc.count() > 0:
        return "row", loc
    for sel in ANCHOR_SELECTORS:
        loc = page.locator(sel)
        if loc.count() > 0:
            return "anchor", loc
    return None, None


def parse_size_mb(text: str) -> float | None:
    """行のテキストから「272.7 MB」等を MB 単位の数値にして返す。"""
    m = SIZE_RE.search(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = m.group(2).upper()
    return value / 1024 if unit == "KB" else value * 1024 if unit == "GB" else value


def goto_quota_page(page, url: str = QUOTA_URL) -> None:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    if "accounts.google.com" in page.url:
        raise SystemExit(
            "ログインしていません。先に `python gphotos_cleanup.py login` を実行してください。"
        )


def resolve_list_url(args) -> str:
    """処理対象の一覧ページ URL。--url 指定があればそれを使う(photos.google.com 限定)。"""
    url = getattr(args, "url", None) or QUOTA_URL
    if not url.startswith("https://photos.google.com/"):
        raise SystemExit(
            "--url には photos.google.com のストレージ管理ページの URL を指定してください。"
        )
    return url


def open_first_tile(page, interactive: bool = True) -> bool:
    """グリッド先頭のメディアをビューアで開く。対象が無ければ False。

    リンク型タイルなら自動でクリックする。チェックボックス型タイル
    (クリックが「選択」になり誤削除につながる)や検出できない場合は、
    ユーザーに最初の 1 枚を手動で開いてもらう(interactive=True のとき)。
    """
    page.wait_for_timeout(2_000)
    kind, tiles = find_tiles(page)

    if kind == "row":
        # リスト行の「開く」ボタンをクリックしてビューアを開く。
        # 行自体のクリックは「選択」トグルになり誤削除につながるため使わない。
        try:
            open_btn = tiles.first.get_by_role("button", name=OPEN_BUTTON_RE)
            open_btn.first.click(timeout=10_000)
            page.wait_for_url(lambda url: "photo/" in url, timeout=15_000)
            page.wait_for_timeout(1_000)
            return True
        except PWTimeoutError:
            pass  # 手動フォールバックへ
    elif kind == "anchor":
        tiles.first.click()
        try:
            page.wait_for_url(lambda url: "photo/" in url, timeout=15_000)
            page.wait_for_timeout(1_000)
            return True
        except PWTimeoutError:
            pass  # 手動フォールバックへ

    if not interactive:
        return False

    print()
    if kind is None:
        # タイルを自動検出できない(UI の DOM が想定と違う)か、対象が無くなったかのどちらか。
        print("メディアの一覧を自動検出できませんでした。")
        print("画面にまだ写真/動画が残っている場合は、60 秒以内に最初の 1 枚を")
        print("手動でクリックして大きく表示してください(以降は自動で処理します)。")
        print("残っていない場合は、そのまま待つと終了します。")
        timeout_ms = 60_000
    else:
        print("最初の写真/動画を、ブラウザで手動でクリックして大きく表示してください。")
        print("(1 枚を大きく表示するビューアが開けば OK です。以降は自動で処理します)")
        timeout_ms = 180_000

    try:
        page.wait_for_url(lambda url: "photo/" in url, timeout=timeout_ms)
        page.wait_for_timeout(1_000)
        return True
    except PWTimeoutError:
        return False


def unique_path(directory: Path, filename: str) -> Path:
    """同名ファイルがある場合は `name (1).ext` の形で回避する。"""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for i in range(1, 10_000):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"保存先ファイル名を確保できません: {filename}")


def download_current(page, incoming_dir: Path, timeout_s: int) -> Path | None:
    """ビューアに表示中のメディアを Shift+D でダウンロードし、保存できたら Path を返す。

    ダウンロード URL が一時的に 404 を返すことがある(ブラウザにエラーページが
    表示される)。その場合は None を返し、呼び出し側が掃除とスキップを行う。
    ブラウザ自体が落ちた場合は PWError を送出し、呼び出し側で再起動する。
    """
    for attempt in (1, 2):
        try:
            with page.expect_download(timeout=timeout_s * 1_000) as dl_info:
                page.keyboard.press("Shift+KeyD")
            download = dl_info.value
            dest = unique_path(incoming_dir, download.suggested_filename)
            download.save_as(dest)  # 完了まで待つ
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            log(f"  ダウンロードしたファイルが空です: {dest}")
        except PWTimeoutError:
            log(f"  ダウンロードがタイムアウトしました (試行 {attempt}/2)")
        except PWError as e:
            log(f"  ダウンロード中のエラー (試行 {attempt}/2): {e}")
            if is_closed_error(e):
                raise  # ブラウザごと落ちている → 呼び出し側で再起動
        except Exception as e:  # noqa: BLE001
            log(f"  ダウンロード中のエラー (試行 {attempt}/2): {e}")
        try:
            page.wait_for_timeout(5_000)
        except PWError:
            raise
    return None


def delete_current(page) -> bool:
    """ビューアに表示中のメディアをゴミ箱へ移動する。成功で True。

    # キーで確認ダイアログ「アイテムをゴミ箱に移動します」(キャンセル / OK)を開き、
    OK を完全一致で照合してクリックする(キャンセルは絶対に押さない)。
    ショートカットが効かない画面ではツールバーの「ゴミ箱に移動」ボタンを経由する。
    成功の判定は「URL の変化(次のメディアへ進む/一覧へ戻る)」または
    「完了トースト(ゴミ箱に移動しました)の表示」のどちらかで行う。
    """
    prev_url = page.url
    dialogs = page.locator('[role="dialog"], [role="alertdialog"]')
    toast = page.get_by_text(TRASH_DONE_RE).first

    def find_confirm_button():
        """確認ダイアログの OK ボタンを探す(ダイアログ内 → 画面全体の可視 OK の順)。"""
        try:
            btn = dialogs.get_by_role("button", name=DIALOG_OK_RE)
            if btn.count() > 0:
                return btn.first
            # ダイアログに role が付かない UI へのフォールバック(OK 完全一致のみ)
            btn = page.get_by_role("button", name=OK_ONLY_RE)
            for i in range(btn.count()):
                if btn.nth(i).is_visible():
                    return btn.nth(i)
        except PWError:
            pass
        return None

    def wait_confirm_button(seconds: float):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            found = find_confirm_button()
            if found is not None:
                return found
            page.wait_for_timeout(400)
        return None

    # OK クリックが空振りすることがある(ダイアログ表示直後など)ため、最大 3 回試行する。
    # URL(=表示中のメディア)が変わっていない間だけ再試行するので、別のアイテムを
    # 誤って削除することはない。
    for attempt in (1, 2, 3):
        if page.url != prev_url:
            # すでに次へ進んでいた(前回の試行が実は成功していた)
            page.wait_for_timeout(1_000)
            return True

        # 確認ダイアログを開く(前回試行の残りがあればそれを使う)
        confirm = find_confirm_button()
        if confirm is None:
            page.keyboard.press("#")
            confirm = wait_confirm_button(6)
        if confirm is None:
            # ショートカットが効かない場合: ツールバーのゴミ箱ボタン経由で開く
            try:
                page.get_by_role("button", name=TRASH_CONFIRM_RE).first.click(
                    timeout=5_000
                )
            except (PWTimeoutError, PWError):
                log("  削除ボタン/確認ダイアログが見つかりませんでした")
                page.keyboard.press("Escape")
                return False
            confirm = wait_confirm_button(6)
        if confirm is None:
            log("  確認ダイアログの OK ボタンが見つかりませんでした")
            page.keyboard.press("Escape")
            return False

        try:
            confirm.click(timeout=5_000)
        except (PWTimeoutError, PWError):
            log(f"  OK ボタンのクリックに失敗しました(試行 {attempt}/3)")
            continue

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if page.url != prev_url:
                # 次のメディアへ進んだ、または一覧へ戻った → 削除成功
                page.wait_for_timeout(1_000)
                return True
            try:
                if toast.is_visible():
                    # 削除は成功したが URL が変わらない UI → ビューアを閉じて一覧から続行
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(1_000)
                    return True
            except PWError:
                pass
            page.wait_for_timeout(500)

        if attempt < 3:
            log(f"  削除の確認が取れませんでした(試行 {attempt}/3)→ 再試行します")

    log("  削除の完了を確認できませんでした")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    return False


def advance_without_delete(page) -> bool:
    """削除せずに次のメディアへ進む。進めなければ(=最後なら) False。"""
    prev_url = page.url
    page.keyboard.press("ArrowRight")
    try:
        page.wait_for_url(lambda url: url != prev_url, timeout=8_000)
    except PWTimeoutError:
        return False
    page.wait_for_timeout(500)
    return True


def save_error_screenshot(page, out_dir: Path, tag: str) -> None:
    try:
        errors_dir = out_dir / "errors"
        errors_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(errors_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{tag}.png")
        )
    except Exception:  # noqa: BLE001
        pass


class BatchManager:
    """ダウンロードしたファイルを容量上限つきのフォルダ (batch_001, batch_002, ...) に振り分ける。

    フォルダを他のドライブへ移動しても番号を再利用しないよう、
    これまでに使った最大の番号を state.json に記録しておく。
    """

    def __init__(self, out_dir: Path, cap_bytes: int):
        self.out_dir = out_dir
        self.cap = cap_bytes
        self.state_path = out_dir / "state.json"

        # ダウンロード完了までの一時置き場(中断で残った中途半端なファイルは掃除する)
        self.incoming = out_dir / "_incoming"
        self.incoming.mkdir(parents=True, exist_ok=True)
        for leftover in self.incoming.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)

        highest = 0
        if self.state_path.exists():
            try:
                highest = int(json.loads(self.state_path.read_text()).get("highest_batch", 0))
            except (ValueError, OSError, json.JSONDecodeError):
                highest = 0
        for d in out_dir.glob("batch_*"):
            m = re.fullmatch(r"batch_(\d+)", d.name)
            if d.is_dir() and m:
                highest = max(highest, int(m.group(1)))

        if highest and (out_dir / f"batch_{highest:03d}").is_dir():
            # 前回のフォルダがまだ残っている → 続きから詰める
            self.index = highest
            self.bytes = sum(
                f.stat().st_size for f in self.folder.rglob("*") if f.is_file()
            )
        else:
            # 初回、または前回のフォルダが移動済み → 新しい番号で開始
            self.index = highest + 1
            self.bytes = 0
        self._save_state()

    @property
    def folder(self) -> Path:
        return self.out_dir / f"batch_{self.index:03d}"

    def place(self, tmp_path: Path) -> Path:
        """一時置き場のファイルを、上限を超えない batch フォルダへ移動する。"""
        size = tmp_path.stat().st_size
        if self.bytes > 0 and self.bytes + size > self.cap:
            self.index += 1
            self.bytes = 0
            log(f"  フォルダの上限に達したため {self.folder.name}/ に切り替えます。")
        self.folder.mkdir(parents=True, exist_ok=True)
        dest = unique_path(self.folder, tmp_path.name)
        shutil.move(str(tmp_path), str(dest))
        self.bytes += size
        self._save_state()
        return dest

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps({"highest_batch": self.index}))


def wait_for_free_space(out_dir: Path, min_free_bytes: int) -> bool:
    """空き容量が確保されるまで待つ。ユーザーが中止を選んだら False。"""
    while True:
        free = shutil.disk_usage(out_dir).free
        if free >= min_free_bytes:
            return True
        print()
        print(f"⚠ ディスクの空き容量が少なくなりました(残り {free / 1_073_741_824:.1f} GB)。")
        print(f"  {out_dir} の batch_XXX フォルダを別のドライブへ移動して空きを作ってください。")
        print("  (移動済みフォルダの番号は再利用されないので、そのまま移動して構いません)")
        answer = input("  空きを作ったら Enter、終了する場合は q + Enter: ")
        if answer.strip().lower() == "q":
            return False


class Manifest:
    """処理結果を CSV に追記していく記録簿。"""

    FIELDS = ["timestamp", "filename", "size_bytes", "folder", "action"]

    def __init__(self, out_dir: Path):
        self.path = out_dir / "manifest.csv"
        new_file = not self.path.exists()
        self._fh = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.FIELDS)
        if new_file:
            self._writer.writeheader()

    def record(
        self, filename: str, size_bytes: int | str, action: str, folder: str = ""
    ) -> None:
        self._writer.writerow(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "filename": filename,
                "size_bytes": size_bytes,
                "folder": folder,
                "action": action,
            }
        )
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def cmd_login(_args) -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = launch_context(p)
        page = get_page(ctx)
        page.goto(PHOTOS_URL)
        print()
        print("開いたブラウザで Google アカウントにログインしてください。")
        print("Google フォトのライブラリが表示されたらログイン完了です。")
        input("ログインが完了したら、このターミナルで Enter を押してください... ")
        ctx.close()
    log("ログイン情報を profile/ に保存しました。次は scan または run を実行できます。")


def cmd_scan(args) -> None:
    """対象(容量を消費しているメディア)の一覧をスクロールしながら収集する。"""
    list_url = resolve_list_url(args)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    scan_path = out_dir / "scan.csv"

    with sync_playwright() as p:
        ctx = launch_context(p)
        page = get_page(ctx)
        goto_quota_page(page, list_url)
        page.wait_for_timeout(3_000)

        detected_kind = None
        seen: dict[str, tuple[str, str, float | None]] = {}  # key -> (date, text, size_mb)
        stagnant_rounds = 0
        while stagnant_rounds < 5:
            before = len(seen)
            kind, tiles = find_tiles(page)
            if kind:
                detected_kind = detected_kind or kind
                for tile in tiles.all():
                    try:
                        if kind == "row":
                            key = tile.get_attribute("data-media-key") or ""
                            text = " ".join((tile.inner_text() or "").split())
                        else:
                            key = tile.get_attribute("href") or ""
                            text = tile.get_attribute("aria-label") or ""
                    except Exception:  # noqa: BLE001  # 仮想スクロールで要素が消えた場合
                        continue
                    if key and key not in seen:
                        date_m = DATE_RE.search(text)
                        seen[key] = (
                            date_m.group(0) if date_m else "",
                            text,
                            parse_size_mb(text),
                        )
            # 仮想スクロールのリストを下へ送る(リスト上にマウスを置いてホイール)
            page.mouse.move(800, 450)
            page.mouse.wheel(0, 2_000)
            page.keyboard.press("PageDown")
            page.wait_for_timeout(800)
            stagnant_rounds = stagnant_rounds + 1 if len(seen) == before else 0

        ctx.close()

    with scan_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "size_mb", "text", "media_key"])
        for key, (date, text, size_mb) in seen.items():
            writer.writerow([date, f"{size_mb:.1f}" if size_mb else "", text, key])

    total_mb = sum(s for _, _, s in seen.values() if s)
    log(f"容量を消費しているメディア: {len(seen)} 件(概算)")
    if total_mb:
        log(f"合計サイズ: 約 {total_mb / 1024:.2f} GB")
        log(f"→ 15GB ごとのフォルダ分割で約 {int(total_mb / 1024 / 15) + 1} フォルダになる見込みです。")
    log(f"一覧を書き出しました: {scan_path}")
    if detected_kind:
        log(f"検出した一覧の種類: {detected_kind}")
    if not seen:
        log("一覧を自動検出できませんでした。ブラウザ上に対象が表示されているのに 0 件になる場合は、")
        log("`python gphotos_cleanup.py debug` を実行し、表示される診断結果を共有してください。")
        log("(scan が 0 件でも、run は手動で最初の 1 枚を開けばそのまま使えます)")


def cmd_debug(args) -> None:
    """ストレージ管理ページの DOM を診断し、タイル検出の手がかりを出力する。"""
    list_url = resolve_list_url(args)
    out_dir = Path(args.out).expanduser().resolve()
    debug_dir = out_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    def trunc(s: str, n: int = 90) -> str:
        return s if len(s) <= n else s[: n - 3] + "..."

    with sync_playwright() as p:
        ctx = launch_context(p)
        page = get_page(ctx)
        goto_quota_page(page, list_url)
        page.wait_for_timeout(5_000)

        print()
        print("―――― 診断結果(この出力をそのまま共有してください)――――")
        print(f"URL: {page.url}")
        print(f"タイトル: {page.title()}")

        candidates = [
            (ROW_SELECTOR, "メディア行 (data-media-key)"),
            ('a[href*="quotamanagement"][href*="photo/"]', "quotamanagement リンク"),
            ('a[href*="photo/"]', "photo/ リンク"),
            ('div[role="checkbox"][aria-label]', "チェックボックス型タイル"),
            ("a[href]", "リンク全体"),
            ('[role="link"]', "role=link"),
            ('[role="listitem"]', "role=listitem"),
            ('[role="option"]', "role=option"),
            ("img", "img 要素"),
            ("iframe", "iframe"),
        ]
        for sel, desc in candidates:
            try:
                print(f"  {desc:<24} {sel:<48} : {page.locator(sel).count()} 件")
            except Exception as e:  # noqa: BLE001
                print(f"  {desc:<24} {sel:<48} : エラー {e}")

        print("  --- a[href] のサンプル(先頭 10 件) ---")
        for a in page.locator("a[href]").all()[:10]:
            href = a.get_attribute("href") or ""
            label = a.get_attribute("aria-label") or ""
            print(f"    href={trunc(href)}  aria-label={trunc(label, 60)}")

        print("  --- role=checkbox のサンプル(先頭 5 件) ---")
        for c in page.locator('[role="checkbox"]').all()[:5]:
            label = c.get_attribute("aria-label") or ""
            print(f"    aria-label={trunc(label, 120)}")

        print("  --- メディア行のサンプル(先頭 3 件) ---")
        for r in page.locator(ROW_SELECTOR).all()[:3]:
            key = r.get_attribute("data-media-key") or ""
            text = " ".join((r.inner_text() or "").split())
            print(f"    key={trunc(key, 50)}  text={trunc(text, 60)}")

        screenshot = debug_dir / "screenshot.png"
        html = debug_dir / "page.html"
        page.screenshot(path=str(screenshot))
        html.write_text(page.content(), encoding="utf-8")
        print(f"  スクリーンショット: {screenshot}")
        print(f"  ページ HTML: {html}")
        print("――――――――――――――――――――――――――――――")
        print("※ ファイル名などの個人情報が含まれるため、共有する際は必要な範囲だけにしてください。")

        ctx.close()


def cmd_run(args) -> None:
    list_url = resolve_list_url(args)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(out_dir)
    batches = BatchManager(out_dir, cap_bytes=int(args.batch_size_gb * 1_073_741_824))
    min_free_bytes = int(args.min_free_gb * 1_073_741_824)

    processed = 0
    downloaded_bytes = 0
    failures = 0
    consecutive_failures = 0

    with sync_playwright() as p:
        ctx = launch_context(p)
        page = get_page(ctx)
        goto_quota_page(page, list_url)

        if not open_first_tile(page):
            log("容量を消費しているメディアが見つかりませんでした。処理するものはありません。")
            ctx.close()
            manifest.close()
            return

        log(f"処理を開始します(保存先: {out_dir}、現在のフォルダ: {batches.folder.name}/)")
        log(f"1 フォルダあたり {args.batch_size_gb:g} GB まで、空き容量 {args.min_free_gb:g} GB を下回ると一時停止します。")
        if args.no_delete:
            log("--no-delete が指定されているため、削除は行いません。")

        while True:
            if args.limit and processed >= args.limit:
                log(f"--limit {args.limit} に達したため終了します。")
                break
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log("連続で失敗したため安全のため中断します。errors/ の画像を確認してください。")
                break
            if not wait_for_free_space(out_dir, min_free_bytes):
                log("ユーザーの操作により終了します。ここまでの結果は manifest.csv にあります。")
                break

            try:
                cleanup_stray_pages(ctx, page)
                if not in_viewer(page):
                    # ビューアが閉じた/エラーページに飛ばされた → 一覧に戻ってやり直す
                    goto_quota_page(page, list_url)
                    if not open_first_tile(page):
                        log("残りの対象はありません。")
                        break

                tmp = download_current(page, batches.incoming, args.download_timeout)
                if tmp is None:
                    failures += 1
                    consecutive_failures += 1
                    manifest.record("(不明)", "", "download_failed")
                    save_error_screenshot(page, out_dir, "download_failed")
                    log("  ダウンロードに失敗したため、この項目は削除せずスキップします。")
                    if in_viewer(page):
                        if not advance_without_delete(page):
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1_000)
                    continue

                dest = batches.place(tmp)
                size = dest.stat().st_size
                downloaded_bytes += size
                consecutive_failures = 0
                processed += 1
                folder = dest.parent.name
                log(f"[{processed}] 保存しました: {folder}/{dest.name} ({size / 1_048_576:.1f} MB)")

                if args.no_delete:
                    manifest.record(dest.name, size, "downloaded", folder)
                    if not advance_without_delete(page):
                        log("最後のメディアに到達しました。")
                        break
                else:
                    if delete_current(page):
                        manifest.record(dest.name, size, "downloaded_and_trashed", folder)
                        log("      → ゴミ箱へ移動しました。")
                    else:
                        failures += 1
                        manifest.record(dest.name, size, "downloaded_delete_failed", folder)
                        save_error_screenshot(page, out_dir, "delete_failed")
                        log("      → ゴミ箱への移動に失敗しました(ファイルは保存済み)。")
                        if not advance_without_delete(page):
                            break

                if args.pause > 0:
                    page.wait_for_timeout(int(args.pause * 1_000))

            except PWError as e:
                failures += 1
                consecutive_failures += 1
                if is_closed_error(e):
                    # ダウンロード連発時の一時的な 404 などを引き金にブラウザごと
                    # 落ちることがある → 再起動して続きから(未削除分は一覧に残っている)
                    log("ブラウザとの接続が失われました。ブラウザを再起動して続行します。")
                    try:
                        ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        ctx = launch_context(p)
                        page = get_page(ctx)
                        goto_quota_page(page, list_url)
                        page.wait_for_timeout(3_000)
                    except Exception as e2:  # noqa: BLE001
                        log(f"ブラウザの再起動に失敗しました: {e2}")
                        break
                else:
                    log(f"予期しないエラーが発生しました: {e}")
                    save_error_screenshot(page, out_dir, "unexpected")
                    try:
                        page.wait_for_timeout(2_000)
                    except PWError:
                        pass

        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass

    manifest.close()
    log("―――― 結果 ――――")
    log(f"ダウンロード: {processed} 件 / {downloaded_bytes / 1_073_741_824:.2f} GB")
    if failures:
        log(f"失敗・スキップ: {failures} 件(詳細は {manifest.path})")
    if not args.no_delete and processed:
        log("削除したメディアはゴミ箱に 60 日間残り、その間は容量を消費したままです。")
        log("すぐに容量を解放するには https://photos.google.com/trash でゴミ箱を空にしてください。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Google フォトで容量を消費している写真・動画をダウンロードして削除する",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="ブラウザを開いて Google にログインする(初回のみ)")

    url_help = (
        "処理対象の一覧ページ URL(既定: サイズの大きい写真と動画)。"
        "photos.google.com/storage の「確認して削除」の各カテゴリ"
        "(スクリーンショットと録画、ぼやけた写真など)の URL も指定できる"
    )

    p_scan = sub.add_parser("scan", help="対象の一覧を確認する(ダウンロードも削除もしない)")
    p_scan.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="出力ディレクトリ")
    p_scan.add_argument("--url", default=QUOTA_URL, help=url_help)

    p_debug = sub.add_parser(
        "debug", help="タイル検出がうまくいかない場合の診断情報を出力する"
    )
    p_debug.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="出力ディレクトリ")
    p_debug.add_argument("--url", default=QUOTA_URL, help=url_help)

    p_run = sub.add_parser("run", help="ダウンロードして(オプションで)ゴミ箱へ移動する")
    p_run.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="保存先ディレクトリ")
    p_run.add_argument("--limit", type=int, default=0, help="処理する最大件数(0 = 無制限)")
    p_run.add_argument(
        "--no-delete", action="store_true", help="ダウンロードのみ行い、削除しない"
    )
    p_run.add_argument(
        "--download-timeout",
        type=int,
        default=900,
        help="1 件あたりのダウンロード待ち時間の上限(秒、既定 900)",
    )
    p_run.add_argument(
        "--batch-size-gb",
        type=float,
        default=15.0,
        help="1 フォルダ (batch_XXX) あたりの合計サイズ上限 GB(既定 15)",
    )
    p_run.add_argument(
        "--min-free-gb",
        type=float,
        default=5.0,
        help="ディスク空き容量がこの GB を下回ると一時停止してフォルダ移動を促す(既定 5)",
    )
    p_run.add_argument(
        "--pause",
        type=float,
        default=2.0,
        help="1 件処理するごとの待機秒。連続アクセスによる一時的なエラーを抑える(既定 2)",
    )
    p_run.add_argument("--url", default=QUOTA_URL, help=url_help)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "login":
        cmd_login(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "debug":
        cmd_debug(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中断しました。ここまでの結果は manifest.csv に記録されています。")
