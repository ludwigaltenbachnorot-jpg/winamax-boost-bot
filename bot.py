#!/usr/bin/env python3

import json, os, re, hashlib, logging, requests

CONFIG = {
    "telegram_token":   os.environ["TELEGRAM_TOKEN"],
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "8677600827"),
}

COOKIES = os.environ["WINAMAX_COOKIES"]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"

SEEN_FILE = "seen.json"
SUBSCRIBERS_FILE = "winamax_subscribers.json"

MAX_STAKE_FILTER = "10"

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
        log.error(f"Erreur page Winamax : {e}")
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

            m = re.search(r"mise max (\d+)\s*€", bet_title, re.I) or re.search(r"(\d+)\s*€\s*maximum", bet_help, re.I)
            max_mise = m.group(1) if m else None

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
                "site": "Winamax",
            })

    except Exception as e:
        log.error(f"Erreur extraction Winamax : {e}")

    return boosts


def get_betclic_state():
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, locale="fr-FR")
            page = ctx.new_page()
            resp = page.goto("https://www.betclic.fr/cotes-boostees", timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            raw = page.evaluate(
                "() => { const el = document.getElementById('ng-state'); return el ? el.textContent : null; }"
            )
            if not raw:
                log.warning(f"ng-state Betclic vide ou absent | status={resp.status if resp else 'N/A'} | url={page.url} | titre={page.title()!r}")
                html = page.content()
                log.warning(f"debut HTML : {html[:500]!r}")
            browser.close()
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        log.error(f"Erreur page Betclic : {e}")
        return None


def extract_betclic_boosts(data):
    boosts = []
    if not data:
        return boosts

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "boostedOdds" and isinstance(v, list):
                    for item in v:
                        yield item
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)

    try:
        seen_keys = set()
        for item in walk(data):
            key = (item.get("matchId"), item.get("selectionId"), item.get("odds"))
            if key in seen_keys:
                continue
            seen_keys.add(key)

            match_name = item.get("matchName", "")
            title = item.get("title", "")
            selection = item.get("selectionName", "")
            odd = item.get("odds", 0)
            prev_odd = item.get("previousOdds", 0)
            max_stake = item.get("maxStake")

            pct = round(((odd - prev_odd) / prev_odd * 100) if prev_odd else 0, 1)

            label = f"{title} - {selection}" if title else selection
            boost_id = hashlib.md5(f"betclic{match_name}{label}{odd}".encode()).hexdigest()

            boosts.append({
                "id": boost_id,
                "title": match_name,
                "label": label,
                "odd": odd,
                "prev_odd": prev_odd,
                "pct": pct,
                "max_mise": str(int(max_stake)) if max_stake else None,
                "site": "Betclic",
            })
    except Exception as e:
        log.error(f"Erreur extraction Betclic : {e}")

    return boosts


def main():
    seen = set(load_json(SEEN_FILE, []))
    subs = load_json(SUBSCRIBERS_FILE, {"chat_ids": [CONFIG["telegram_chat_id"]], "last_update_id": 0})
    chat_ids = set(subs.get("chat_ids", [CONFIG["telegram_chat_id"]]))
    last_update_id = subs.get("last_update_id", 0)

    chat_ids, last_update_id = check_new_subscribers(chat_ids, last_update_id)

    log.info("Verification Winamax...")
    winamax_boosts = extract_boosts(get_initial_state())
    log.info(f"{len(winamax_boosts)} boost(s) Winamax trouve(s)")

    log.info("Verification Betclic...")
    betclic_boosts = extract_betclic_boosts(get_betclic_state())
    log.info(f"{len(betclic_boosts)} boost(s) Betclic trouve(s)")

    boosts = [b for b in (winamax_boosts + betclic_boosts) if b["max_mise"] == MAX_STAKE_FILTER]
    log.info(f"{len(boosts)} boost(s) a {MAX_STAKE_FILTER}€ apres filtre")

    for b in boosts:
        if b["id"] in seen:
            continue
        seen.add(b["id"])

        emoji = "🟠" if b["site"] == "Winamax" else "🔵"
        msg = f"🚀 BOOST {b['site'].upper()} {emoji}\n"
        msg += f"🏟 {b['title']}\n"
        msg += f"📌 {b['label']}\n"
        msg += f"📈 {b['prev_odd']} → {b['odd']}"
        if b["pct"]:
            msg += f" (+{b['pct']}%)"
        msg += f"\n💰 Mise max {b['max_mise']}€"

        log.info(f"Nouveau [{b['site']}] : {b['title']} | {b['label']} | {b['odd']}")
        send_telegram(msg, chat_ids)

    save_json(SEEN_FILE, list(seen))
    save_json(SUBSCRIBERS_FILE, {"chat_ids": list(chat_ids), "last_update_id": last_update_id})


if __name__ == "__main__":
    main()
