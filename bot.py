#!/usr/bin/env python3
"""בוט מסירות: בודק מודעות חדשות באגורה ושולח התראה לטלגרם.

רץ ב-GitHub Actions כל 15 דקות. ההגדרות (ערים, פריטים, מידות) ב-config.json.
המודעות שכבר נבדקו נשמרות ב-state.json כדי שלא תקבל אותה מודעה פעמיים.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
STATE = os.path.join(ROOT, "state.json")
BASE = "https://www.agora.co.il"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url, data=None, timeout=30, tries=3):
    for n in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            raise
        except Exception:  # noqa: BLE001  (תקלת רשת רגעית — מנסים שוב)
            if n == tries - 1:
                raise
            time.sleep(3 * (n + 1))


def norm(s):
    """אותיות בלבד, כדי ש'פתח-תקוה' ו'פתח תקווה' ייחשבו אותו דבר."""
    s = html.unescape(s or "")
    s = re.sub(r"[\"'׳״`\-–_.,()/]", " ", s)
    s = s.replace("וו", "ו").replace("יי", "י")
    return " " + re.sub(r"\s+", " ", s).strip().lower() + " "


# ---------- אגורה ----------

ITEM_RE = re.compile(
    r'<tbody class="objectGroup">(.*?)</tbody>', re.S)


def parse_list(page):
    items = []
    for block in ITEM_RE.findall(page):
        m = re.search(r'href="(/cache/[\d-]+/(\d+)_o\.asp)"', block)
        t = re.search(r'<td class="objectName"><h2><a [^>]*>(.*?)</a>', block, re.S)
        if not m or not t:
            continue
        area = re.search(r'<td class="area">(.*?)</td>', block, re.S)
        cond = re.search(r'<td class="objectState" title="([^"]*)"', block)
        date = re.search(r'<td class="regDate" title="([^"]*)"', block)
        items.append({
            "id": m.group(2),
            "url": BASE + m.group(1),
            "title": html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip(),
            "area": html.unescape(re.sub(r"<[^>]+>", "", area.group(1))).strip() if area else "",
            "condition": html.unescape(cond.group(1)) if cond else "",
            "date": date.group(1) if date else "",
            "photo": 'class="photoIcon"><a' in block,
        })
    return items


def item_description(url):
    try:
        page = fetch(url)
    except Exception as e:  # noqa: BLE001
        print("  ! לא הצלחתי לפתוח", url, e)
        return ""
    m = re.search(r'<meta name="description" content="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else ""


def dims(desc):
    """מחזיר {'רוחב':200,'עומק':95,'גובה':90} מתוך התיאור, אם יש."""
    out = {}
    for key in ("רוחב", "עומק", "גובה", "אורך"):
        m = re.search(key + r"\s*:?\s*(\d+(?:\.\d+)?)", desc)
        if m:
            v = float(m.group(1))
            if v < 10:  # כתבו במטרים
                v *= 100
            out[key] = int(round(v))
    if "אורך" in out and "רוחב" not in out:
        out["רוחב"] = out.pop("אורך")
    return out


# ---------- התאמה ----------

def has_any(text, words):
    """מילה עם רווח בהתחלה/בסוף = מילה שלמה בלבד (למשל " ר ")."""
    for w in words:
        n = norm(w) if (w.startswith(" ") or w.endswith(" ")) else norm(w).strip()
        if n.strip() and n in text:
            return True
    return False


def match(rule, text):
    if rule.get("exclude") and has_any(text, rule["exclude"]):
        return False
    if rule.get("any") and not has_any(text, rule["any"]):
        return False
    if not rule.get("any") and not rule.get("all") and not has_any(text, rule["title"]):
        return False
    for group in rule.get("all", []):
        if not has_any(text, group):
            return False
    return True


def fit_line(rule, d):
    lim = rule.get("max_cm")
    if not lim:
        return ""
    if not d.get("רוחב") and not d.get("עומק"):
        return "📏 לא צוינו מידות"
    shown = " × ".join(f"{d[k]}" for k in ("רוחב", "עומק", "גובה") if d.get(k))
    w, dp = d.get("רוחב"), d.get("עומק")
    ok = True
    if w and w > lim["רוחב"]:
        ok = False
    if dp and dp > lim["עומק"]:
        ok = False
    where = lim.get("איפה", "")
    if ok:
        return f"📏 {shown} ס\"מ ✅ נכנס{(' ל' + where) if where else ''}"
    return f"📏 {shown} ס\"מ ⚠️ גדול מדי{(' ל' + where) if where else ''} (עד {lim['רוחב']}×{lim['עומק']})"


# ---------- טלגרם ----------

def tg(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    return json.loads(fetch(f"https://api.telegram.org/bot{token}/{method}", data=data))


WELCOME = ("✅ <b>בוט המסירות מחובר!</b>\n"
           "מעכשיו תקבל/י כאן התראה על כל מודעה מתאימה באגורה.\n"
           "כדי להפסיק לקבל הודעות שולחים /stop")


def update_chats(token, state, join_code):
    """מוסיף מי שלחץ Start בקישור ההצטרפות, ומסיר מי ששלח /stop."""
    chats = state.setdefault("chats", [])
    try:
        res = tg(token, "getUpdates", offset=state.get("tg_offset", 0), timeout=0)
    except Exception as e:  # noqa: BLE001
        print("! getUpdates נכשל:", e)
        return
    for upd in res.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        cid = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()
        if not cid or chat.get("type") != "private":
            continue
        who = chat.get("first_name") or cid
        if text.startswith("/stop") and cid in chats:
            chats.remove(cid)
            send(token, cid, "👋 הפסקת לקבל הודעות. כדי לחזור, לוחצים שוב על קישור ההצטרפות.")
            print("הוסר מנוי:", who)
        elif text.startswith("/start") and cid not in chats:
            # הראשון מצטרף בלי קוד; כל השאר רק דרך קישור ההצטרפות
            if not chats or (join_code and join_code in text):
                chats.append(cid)
                send(token, cid, WELCOME)
                print("נוסף מנוי:", who)
            else:
                send(token, cid, "🔒 הבוט פרטי. כדי להצטרף צריך את קישור ההצטרפות מבעל הבוט.")


def send(token, chat, text, silent=False):
    try:
        tg(token, "sendMessage", chat_id=chat, text=text, parse_mode="HTML",
           disable_web_page_preview="false",
           disable_notification="true" if silent else "false")
        return True
    except Exception as e:  # noqa: BLE001
        print("! שליחה לטלגרם נכשלה:", e)
        return False


# ---------- ריצה ----------

GENERIC = {"name": "למסירה", "emoji": "📦"}


def is_deal(cfg, it, text):
    """מציאה = חפץ כמו חדש, או חפץ ששווה הרבה (מותג, חשמל יקר, חדש באריזה)."""
    d = cfg.get("deals", {})
    if it.get("condition") in d.get("conditions", []):
        return True
    return has_any(text, d.get("words", []))

def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    seen = state.get("seen", [])
    seen_set = set(seen)

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chats = state.setdefault("chats", [])
    if state.get("chat_id") and state["chat_id"] not in chats:
        chats.append(state.pop("chat_id"))
    env_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env_chat and env_chat not in chats:
        chats.append(env_chat)
    if not token:
        print("חסר TELEGRAM_TOKEN (Settings ← Secrets and variables ← Actions). לא שולח כלום.")
    else:
        try:
            me = tg(token, "getMe").get("result", {})
            print(f"הבוט המחובר: @{me.get('username')} · מנויים: {len(chats)}")
        except Exception as e:  # noqa: BLE001
            print("! המפתח של הבוט לא תקין:", e)
        update_chats(token, state, cfg.get("join_code", ""))
        print(f"מנויים אחרי עדכון: {len(chats)}")
    chat = bool(chats)

    towns = [norm(t) for t in cfg["towns"]]
    found = []
    for p in range(1, cfg.get("pages", 3) + 1):
        url = f"{BASE}/toGet.asp?dealType=1" + (f"&page={p}" if p > 1 else "")
        try:
            items = parse_list(fetch(url))
        except Exception as e:  # noqa: BLE001
            print("! לא הצלחתי לטעון את אגורה:", url, e)
            continue
        print(f"עמוד {p}: {len(items)} מודעות")
        found.extend(items)
        time.sleep(1)

    if not found:
        print("! לא נמצאו מודעות — ייתכן שאגורה שינתה את מבנה הדף.")

    sent = 0
    for it in found:
        if it["id"] in seen_set:
            continue
        area = norm(it["area"])
        in_area = any(t in area or area.strip() in t for t in towns if area.strip())
        title = norm(it["title"])
        # בדיקה ראשונה לפי כותרת, כדי לא לפתוח כל מודעה
        maybe = [r for r in cfg["items"] if has_any(title, r["title"])]
        send_all = cfg.get("send_all", False)
        skip = has_any(title, cfg.get("never", []))
        if not in_area or skip or (not maybe and not send_all):
            if token and chat:
                seen.append(it["id"]); seen_set.add(it["id"])
            continue
        time.sleep(1)
        desc = item_description(it["url"])
        text = norm(it["title"] + " " + desc)
        rule = next((r for r in maybe if match(r, text)), None)
        if not rule and send_all:
            rule = GENERIC
        if not rule:
            if token and chat:
                seen.append(it["id"]); seen_set.add(it["id"])
            continue
        print(f"✓ {rule['name']}: {it['title']} ({it['area']}) {it['url']}")
        if not (token and chat):
            continue
        if sent >= cfg.get("max_per_run", 10):
            break  # השאר יישלחו בריצה הבאה
        d = dims(desc)
        deal = is_deal(cfg, it, text)
        lines = [
            *(["💎 <b>מציאה!</b>"] if deal else []),
            (f"⭐ {rule.get('emoji', '📦')} <b>{html.escape(rule['name'])}</b> ⭐" if rule is not GENERIC
             else f"{rule.get('emoji', '📦')} {html.escape(rule['name'])}"),
            f"<b>{html.escape(it['title'])}</b>",
            f"📍 {html.escape(it['area'])}" + (f" · {html.escape(it['condition'])}" if it["condition"] else ""),
        ]
        fl = fit_line(rule, d)
        if fl:
            lines.append(fl)
        if desc:
            short = desc if len(desc) <= 300 else desc[:300] + "…"
            lines.append(f"<i>{html.escape(short)}</i>")
        lines.append(f'<a href="{it["url"]}">פתיחת המודעה באגורה</a>')
        if [c for c in chats if send(token, c, "\n".join(lines), silent=(rule is GENERIC and not deal))]:
            sent += 1
            seen.append(it["id"]); seen_set.add(it["id"])
        time.sleep(1)

    state["seen"] = seen[-5000:]
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print(f"נשלחו {sent} התראות.")


if __name__ == "__main__":
    sys.exit(main())
