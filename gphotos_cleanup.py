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
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
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

# ゴミ箱移動の確認ダイアログのボタン文言(日本語 / 英語 UI 両対応)
TRASH_CONFIRM_RE = re.compile(
    r"(ゴミ箱に移動|ごみ箱に移動|Move to trash|Move to bin)", re.IGNORECASE
)

MAX_CONSECUTIVE_FAILURES = 3


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


def in_viewer(page) -> bool:
    return "photo/" in page.url


def tile_locator(page):
    return page.locator('a[href*="photo/"]')


def goto_quota_page(page) -> None:
    page.goto(QUOTA_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    if "accounts.google.com" in page.url:
        raise SystemExit(
            "ログインしていません。先に `python gphotos_cleanup.py login` を実行してください。"
        )


def open_first_tile(page) -> bool:
    """グリッド先頭のメディアをクリックしてビューアを開く。対象が無ければ False。"""
    tiles = tile_locator(page)
    try:
        tiles.first.wait_for(state="visible", timeout=10_000)
    except PWTimeoutError:
        return False
    tiles.first.click()
    try:
        page.wait_for_url(lambda url: "photo/" in url, timeout=15_000)
    except PWTimeoutError:
        return False
    page.wait_for_timeout(1_000)
    return True


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


def download_current(page, out_dir: Path, timeout_s: int) -> Path | None:
    """ビューアに表示中のメディアを Shift+D でダウンロードし、保存できたら Path を返す。"""
    for attempt in (1, 2):
        try:
            with page.expect_download(timeout=timeout_s * 1_000) as dl_info:
                page.keyboard.press("Shift+KeyD")
            download = dl_info.value
            dest = unique_path(out_dir, download.suggested_filename)
            download.save_as(dest)  # 完了まで待つ
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            log(f"  ダウンロードしたファイルが空です: {dest}")
        except PWTimeoutError:
            log(f"  ダウンロードがタイムアウトしました (試行 {attempt}/2)")
        except Exception as e:  # noqa: BLE001
            log(f"  ダウンロード中のエラー (試行 {attempt}/2): {e}")
        page.wait_for_timeout(2_000)
    return None


def delete_current(page) -> bool:
    """ビューアに表示中のメディアをゴミ箱へ移動する。成功で True。"""
    prev_url = page.url
    page.keyboard.press("#")
    try:
        button = page.get_by_role("button", name=TRASH_CONFIRM_RE)
        button.first.click(timeout=10_000)
    except PWTimeoutError:
        log("  ゴミ箱移動の確認ダイアログが見つかりませんでした")
        page.keyboard.press("Escape")
        return False
    try:
        # 次のメディアに進む(URL 変化)か、最後の 1 件ならグリッドへ戻る。
        page.wait_for_url(lambda url: url != prev_url, timeout=15_000)
    except PWTimeoutError:
        log("  削除後に画面が切り替わりませんでした")
        return False
    page.wait_for_timeout(1_000)
    return True


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


class Manifest:
    """処理結果を CSV に追記していく記録簿。"""

    FIELDS = ["timestamp", "filename", "size_bytes", "action"]

    def __init__(self, out_dir: Path):
        self.path = out_dir / "manifest.csv"
        new_file = not self.path.exists()
        self._fh = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.FIELDS)
        if new_file:
            self._writer.writeheader()

    def record(self, filename: str, size_bytes: int | str, action: str) -> None:
        self._writer.writerow(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "filename": filename,
                "size_bytes": size_bytes,
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
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    scan_path = out_dir / "scan.csv"

    with sync_playwright() as p:
        ctx = launch_context(p)
        page = get_page(ctx)
        goto_quota_page(page)

        seen: dict[str, str] = {}  # href -> aria-label
        stagnant_rounds = 0
        while stagnant_rounds < 5:
            before = len(seen)
            for tile in tile_locator(page).all():
                href = tile.get_attribute("href") or ""
                if href and href not in seen:
                    seen[href] = tile.get_attribute("aria-label") or ""
            page.keyboard.press("PageDown")
            page.wait_for_timeout(700)
            stagnant_rounds = stagnant_rounds + 1 if len(seen) == before else 0

        ctx.close()

    with scan_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["label", "href"])
        for href, label in seen.items():
            writer.writerow([label, href])

    log(f"容量を消費しているメディア: {len(seen)} 件(概算)")
    log(f"一覧を書き出しました: {scan_path}")
    if not seen:
        log("対象が見つかりませんでした。容量を消費しているメディアが無い可能性があります。")


def cmd_run(args) -> None:
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(out_dir)

    processed = 0
    downloaded_bytes = 0
    failures = 0
    consecutive_failures = 0

    with sync_playwright() as p:
        ctx = launch_context(p)
        page = get_page(ctx)
        goto_quota_page(page)

        if not open_first_tile(page):
            log("容量を消費しているメディアが見つかりませんでした。処理するものはありません。")
            ctx.close()
            manifest.close()
            return

        log(f"処理を開始します(保存先: {out_dir})")
        if args.no_delete:
            log("--no-delete が指定されているため、削除は行いません。")

        while True:
            if args.limit and processed >= args.limit:
                log(f"--limit {args.limit} に達したため終了します。")
                break
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log("連続で失敗したため安全のため中断します。errors/ の画像を確認してください。")
                break
            if not in_viewer(page):
                # ビューアが閉じた(最後まで到達など)→ グリッドに戻って残りを確認
                goto_quota_page(page)
                if not open_first_tile(page):
                    log("残りの対象はありません。")
                    break

            dest = download_current(page, out_dir, args.download_timeout)
            if dest is None:
                failures += 1
                consecutive_failures += 1
                manifest.record("(不明)", "", "download_failed")
                save_error_screenshot(page, out_dir, "download_failed")
                log("  ダウンロードに失敗したため、この項目は削除せずスキップします。")
                if not advance_without_delete(page):
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(1_000)
                continue

            size = dest.stat().st_size
            downloaded_bytes += size
            consecutive_failures = 0
            processed += 1
            log(f"[{processed}] 保存しました: {dest.name} ({size / 1_048_576:.1f} MB)")

            if args.no_delete:
                manifest.record(dest.name, size, "downloaded")
                if not advance_without_delete(page):
                    log("最後のメディアに到達しました。")
                    break
            else:
                if delete_current(page):
                    manifest.record(dest.name, size, "downloaded_and_trashed")
                    log("      → ゴミ箱へ移動しました。")
                else:
                    failures += 1
                    manifest.record(dest.name, size, "downloaded_delete_failed")
                    save_error_screenshot(page, out_dir, "delete_failed")
                    log("      → ゴミ箱への移動に失敗しました(ファイルは保存済み)。")
                    if not advance_without_delete(page):
                        break

        ctx.close()

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

    p_scan = sub.add_parser("scan", help="対象の一覧を確認する(ダウンロードも削除もしない)")
    p_scan.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="出力ディレクトリ")

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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "login":
        cmd_login(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中断しました。ここまでの結果は manifest.csv に記録されています。")
