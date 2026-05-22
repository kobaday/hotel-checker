#!/usr/bin/env python3
"""
空室チェッカー
指定ホテル・指定部屋の空室状況を定期確認し、変化があれば ntfy.sh で通知する。"""

import json
import os
import sys
import hashlib
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

POST_URL = "https://www6.489pro.com/asp/489/menu.asp"

# 連続エラー何回で通知するか
ERROR_NOTIFY_THRESHOLD = 3


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_available": False,
        "last_html_hash": None,
        "consecutive_errors": 0,
        "consecutive_html_errors": 0,
        "last_checked": None,
    }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_availability(config: dict) -> dict:
    """
    ホテルページにPOSTして空室状況を確認する。

    戻り値:
        {
            "available": bool | None,   # None = 構造異常
            "html_ok": bool,            # ページ構造が正常か
            "html_hash": str | None,    # ypro_rk_all セクションのハッシュ
            "rooms": list[str],         # 取得できた部屋名一覧
            "error": str | None,        # エラーメッセージ
            "checked_at": str,
        }
    """
    target_date = datetime.strptime(config["check_date"], "%Y-%m-%d")
    target_keyword = config.get("target_room_keyword", "")

    params = {
        "id": config["hotel_id"],
        "fn": "room",
        "lan": "JPN",
        "list": "YES",
        "ty": "ser",
    }
    data = {
        "obj_search_from": "1",
        "obj_search_form": "1",
        "lng": "1",
        "obj_year": str(target_date.year),
        "obj_month": str(target_date.month),
        "obj_day": str(target_date.day),
        "obj_per_num": str(config.get("adults", 2)),
        "obj_stay_num": str(config.get("nights", 1)),
        "obj_room_num": str(config.get("rooms", 1)),
        **{f"child_name_{i}": "" for i in range(1, 11)},
        **{f"obj_child_num_{i}": "" for i in range(1, 11)},
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/x-www-form-urlencoded",
    }

    JST = timezone(timedelta(hours=9))
    checked_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    try:
        resp = requests.post(
            POST_URL, params=params, data=data, headers=headers, timeout=30
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return {
            "available": None,
            "html_ok": False,
            "html_hash": None,
            "rooms": [],
            "error": str(e),
            "checked_at": checked_at,
        }

    # UTF-8 でデコード（失敗時はShift-JISにフォールバック）
    try:
        html = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        html = resp.content.decode("shift_jis", errors="replace")

    soup = BeautifulSoup(html, "html.parser")

    # ypro_rk_all 要素（部屋名リスト）を取得
    room_elements = soup.find_all("p", class_="ypro_rk_all")

    if not room_elements:
        # ページ構造が変化している
        html_hash = hashlib.md5(html.encode()).hexdigest()
        page_title = soup.title.get_text(strip=True) if soup.title else "(タイトルなし)"
        return {
            "available": None,
            "html_ok": False,
            "html_hash": html_hash,
            "rooms": [],
            "error": None,
            "checked_at": checked_at,
            "status_code": resp.status_code,
            "page_title": page_title,
        }

    # 部屋名一覧を収集
    rooms = [el.get_text(strip=True) for el in room_elements if el.get_text(strip=True)]

    # ypro_rk_all セクション全体のハッシュ（構造変化検知用）
    section_text = "".join(rooms)
    html_hash = hashlib.md5(section_text.encode()).hexdigest()

    available = any(target_keyword in room for room in rooms)

    return {
        "available": available,
        "html_ok": True,
        "html_hash": html_hash,
        "rooms": rooms,
        "error": None,
        "checked_at": checked_at,
    }


def send_ntfy(topic: str, title: str, message: str, priority: str = "default") -> None:
    """ntfy.sh で通知を送信する。"""
    priority_map = {"urgent": 5, "high": 4, "default": 3, "low": 2, "min": 1}
    resp = requests.post(
        "https://ntfy.sh",
        json={
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority_map.get(priority, 3),
            "tags": ["hotel"],
        },
        timeout=10,
    )
    resp.raise_for_status()


def main() -> None:
    config = load_config()
    state = load_state()

    ntfy_topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic", "")
    hotel_name = os.environ.get("HOTEL_NAME") or config.get("hotel_name", "ホテル")
    check_date = os.environ.get("CHECK_DATE") or config["check_date"]
    target_keyword = os.environ.get("TARGET_ROOM_KEYWORD") or config.get("target_room_keyword", "")

    # 環境変数で上書きされた値をconfigに反映
    config["check_date"] = check_date
    config["target_room_keyword"] = target_keyword
    if os.environ.get("ADULTS"):
        config["adults"] = int(os.environ.get("ADULTS"))

    print(f"[{datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S JST')}] チェック開始")

    # 深夜帯（JST 1時〜7時）はスキップ
    jst_now = datetime.now(timezone(timedelta(hours=9)))
    if 1 <= jst_now.hour < 7:
        print(f"  深夜帯のためスキップ ({jst_now.hour}時台)")
        return
    print(f"  対象: {hotel_name} / {check_date} / {target_keyword}")

    result = check_availability(config)
    print(f"  確認時刻: {result['checked_at']}")

    # ── エラー発生 ──────────────────────────────
    if result["error"]:
        consecutive = state.get("consecutive_errors", 0) + 1
        print(f"  [ERROR] {result['error']} (連続 {consecutive} 回)")
        if consecutive >= ERROR_NOTIFY_THRESHOLD and ntfy_topic:
            try:
                send_ntfy(
                    ntfy_topic,
                    f"⚠️ 接続エラー ({hotel_name})",
                    f"{hotel_name} への接続が {consecutive} 回連続で失敗しています。\n"
                    f"エラー: {result['error']}\n"
                    f"確認時刻: {result['checked_at']}",
                    priority="high",
                )
                print("  ntfy通知: 接続エラー通知を送信")
            except Exception as e:
                print(f"  ntfy送信失敗: {e}", file=sys.stderr)
        state["consecutive_errors"] = consecutive
        save_state(state)
        return

    # ── HTML構造異常 ──────────────────────────────
    if not result["html_ok"]:
        consecutive_html = state.get("consecutive_html_errors", 0) + 1
        print(f"  [WARN] ページ構造が異常です (hash={result['html_hash'][:8]}...) (連続 {consecutive_html} 回)")
        print(f"  [WARN] ステータス: {result.get('status_code', '?')} / ページタイトル: {result.get('page_title', '?')}")
        if consecutive_html == ERROR_NOTIFY_THRESHOLD and ntfy_topic:
            try:
                send_ntfy(
                    ntfy_topic,
                    f"⚠️ HTMLが変化しました ({hotel_name})",
                    f"{hotel_name} の予約ページ構造が {consecutive_html} 回連続で異常です。\n"
                    f"スクレイパーが正常に動作していないかもしれません。\n"
                    f"手動でサイトをご確認ください。\n"
                    f"確認時刻: {result['checked_at']}",
                    priority="high",
                )
                print("  ntfy通知: HTML変化通知を送信")
            except Exception as e:
                print(f"  ntfy送信失敗: {e}", file=sys.stderr)
        state["consecutive_html_errors"] = consecutive_html
        state["last_html_hash"] = result["html_hash"]
        state["consecutive_errors"] = 0
        save_state(state)
        return

    # ── 正常取得 ──────────────────────────────────
    state["consecutive_errors"] = 0
    state["consecutive_html_errors"] = 0
    is_available = result["available"]
    was_available = state.get("last_available", False)
    prev_hash = state.get("last_html_hash")

    print(f"  部屋一覧: {result['rooms']}")
    print(f"  {target_keyword}: {'【空室あり】' if is_available else '【満室】'}")

    # HTML構造が変化した場合（部屋リスト自体は取得できているが内容が変化）
    if result["html_hash"] != prev_hash and prev_hash is not None:
        # 空室状態の変化とは別にHTML変化を通知
        added = set(result["rooms"]) - set(state.get("last_rooms", []))
        removed = set(state.get("last_rooms", [])) - set(result["rooms"])
        if added or removed:
            print(f"  [INFO] 部屋リスト変化: 追加={added}, 削除={removed}")
            if ntfy_topic and not is_available and not was_available:
                # 空室通知以外のHTML変化のみ単独通知
                try:
                    send_ntfy(
                        ntfy_topic,
                        f"ℹ️ 部屋リストが変化 ({hotel_name})",
                        f"部屋の提供状況が変わりました。\n"
                        f"追加: {', '.join(added) or 'なし'}\n"
                        f"削除: {', '.join(removed) or 'なし'}\n"
                        f"確認時刻: {result['checked_at']}",
                    )
                    print("  ntfy通知: 部屋リスト変化通知を送信")
                except Exception as e:
                    print(f"  ntfy送信失敗: {e}", file=sys.stderr)

    # 空室通知（満室 → 空室 になった場合のみ）
    if is_available and not was_available:
        print(f"  >>> {target_keyword} が空きました！")
        if ntfy_topic:
            try:
                send_ntfy(
                    ntfy_topic,
                    f"🏨 空室あり！ {hotel_name}",
                    f"{hotel_name} に空室が出ました！\n"
                    f"部屋: {target_keyword}\n"
                    f"日付: {check_date}\n"
                    f"今すぐ予約: https://www6.489pro.com/asp/489/menu.asp"
                    f"?id={config['hotel_id']}&fn=room&lan=JPN&list=YES&ty=ser\n"
                    f"確認時刻: {result['checked_at']}",
                    priority="urgent",
                )
                print("  ntfy通知: 空室通知を送信")
            except Exception as e:
                print(f"  ntfy送信失敗: {e}", file=sys.stderr)
    elif is_available and was_available:
        print("  (継続して空室あり。再通知はしません)")
    elif not is_available and was_available:
        print(f"  {target_keyword} が満室になりました")

    # 状態を保存
    state["last_available"] = is_available
    state["last_html_hash"] = result["html_hash"]
    state["last_rooms"] = result["rooms"]
    state["last_checked"] = result["checked_at"]
    save_state(state)
    print("  状態を保存しました。")


if __name__ == "__main__":
    main()
