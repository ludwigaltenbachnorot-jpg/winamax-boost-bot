#!/usr/bin/env python3

import json, os, hashlib, logging, requests

CONFIG = {
    "telegram_token":   os.environ["TELEGRAM_TOKEN"],
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "8677600827"),
}

COOKIES = os.environ["WINAMAX_COOKIES"]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"

SEEN_FILE = "seen.json"
SUBSCRIBERS_FILE = "winamax_subscribers.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def send_to(chat_id, msg):
    try:
        url = f"https://api.telegram.org/bot{CONFIG['telegram_token']}/sendMessage"
        r = requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=10)
        if r.status_code == 200:
            log.info(f"Telegram OK ({chat_id})")
        else:
            log.info(f"Telegram erreur {r.status_code} ({chat_id})")
    except Exception as e:
        log.error(f"Telegram KO ({chat_id}) : {e}")


def send_telegram(msg, chat_ids):
    for chat_id in chat_ids:
        send_to(chat_id, msg)


def check_new_subscribers(chat_ids, last_update_id):
    try:
        url = f"https://api.telegram.org/bot{CONFIG['telegram_token']}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 0}
        r = requests.get(url, params=params, timeout=10)
        for update in r.json().get("result", []):
            last_update_id = max(last_update_id, update["update_id"])
            message = update.get("message", {})
            text = message.get("text", "")
            chat_id = str(message.get("chat", {}).get("id", ""))
            if chat_id and text.strip().startswith("/start") and chat_id not in chat_ids:
                chat_ids.add(chat_id)
                log.info(f"Nouvel abonne : {chat_id}")
                send_to(chat_id, "✅ Tu es abonné aux alertes Boost Winamax !")
    except Exception as e:
        log.error(f"Erreur verification abonnes : {e}")
    return chat_ids, last_update_id


def get_initial_state():
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, locale="fr-FR")
            cookies = []
            for part in COOKIES.split("; "):
                if "=" in part:
                    name, _, value = part.partition("=")
                    cookies.append({"name": name, "value": value, "domain": ".winamax.fr", "path": "/"})
            ctx.add_cookies(cookies)
            page = ctx.new_page()
            page.goto("https://www.winamax.fr/paris-sportifs/boosts", timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            raw = page.evaluate("() => JSON.stringify(window.PRELOADED_STATE || null)")
            browser.close()
        if not raw or raw == "null":
            log.warning("PRELOADED_STATE vide ou absent")
            return None
        return json.loads(raw)
    except Exception as e:
        log.error(f"Erreur page : {e}")
        return None


def extract_boosts(data):
    boosts = []
    if not data:
        return boosts
    try:
        bets = data.get("bets", {})
        outcomes = data.get("outcomes", {})
        odds = data.get("odds", {})
        matches = data.get("matches", {})

        for bet_id, bet in bets.items():
            if not bet:
                continue
            bet_title = bet.get("betTitle", "")
            bet_help = bet.get("betTypeHelp", "")

            if "boost" not in bet_title.lower():
                continue

            max_mise = None
            for val in ["10", "20", "50"]:
                if val in bet_title or val in bet_help:
                    max_mise = val
                    break

            match_id = str(bet.get("matchId", ""))
            match_info = matches.get(match_id, {}) or {}
            match_title = match_info.get("title", "")

            outcome_ids = bet.get("outcomes", [])
            outcome_label = ""
            boosted_odd = 0

            for oid in outcome_ids:
                oid_str = str(oid)
                o = outcomes.get(oid_str)
                if o and o.get("label"):
                    outcome_label = o["label"]
                odd_val = odds.get(oid_str)
                if odd_val:
                    boosted_odd = odd_val

            previous_odd = bet.get("previousOdd", 0)
            pct = round(((boosted_odd - previous_odd) / previous_odd * 100) if previous_odd else 0, 1)

            boost_id = hashlib.md5(f"{match_title}{outcome_label}{boosted_odd}".encode()).hexdigest()

            boosts.append({
                "id": boost_id,
                "title": match_title or bet_title,
                "label": outcome_label,
                "odd": boosted_odd,
                "prev_odd": previous_odd,
                "pct": pct,
                "max_mise": max_mise,
            })

    except Exception as e:
        log.error(f"Erreur extraction : {e}")

    return boosts


def main():
    seen = set(load_json(SEEN_FILE, []))
    subs = load_json(SUBSCRIBERS_FILE, {"chat_ids": [CONFIG["telegram_chat_id"]], "last_update_id": 0})
    chat_ids = set(subs.get("chat_ids", [CONFIG["telegram_chat_id"]]))
    last_update_id = subs.get("last_update_id", 0)

    chat_ids, last_update_id = check_new_subscribers(chat_ids, last_update_id)

    log.info("Verification...")
    data = get_initial_state()
    boosts = extract_boosts(data)
    log.info(f"{len(boosts)} boost(s) trouve(s)")

    for b in boosts:
        if b["id"] in seen:
            continue
        seen.add(b["id"])

        msg = f"🚀 BOOST WINAMAX\n"
        msg += f"🏟 {b['title']}\n"
        msg += f"📌 {b['label']}\n"
        msg += f"📈 {b['prev_odd']} → {b['odd']}"
        if b["pct"]:
            msg += f" (+{b['pct']}%)"
        if b["max_mise"]:
            msg += f"\n💰 Mise max {b['max_mise']}€"

        log.info(f"Nouveau : {b['title']} | {b['label']} | {b['odd']}")
        send_telegram(msg, chat_ids)

    save_json(SEEN_FILE, list(seen))
    save_json(SUBSCRIBERS_FILE, {"chat_ids": list(chat_ids), "last_update_id": last_update_id})


if __name__ == "__main__":
    main()
