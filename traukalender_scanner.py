#!/usr/bin/env python3
"""
Traukalender-Scanner Düsseldorf
===============================
Prüft in festen Abständen, ob im Online-Traukalender ein Termin bis zum
Stichtag (Standard: 31.12.2026) frei ist, und schickt eine Push-Nachricht
per ntfy.sh aufs iPad.

WICHTIG
- Das Skript klickt NIE auf "Termin reservieren". Es geht nur bis Schritt 3
  (Terminübersicht), liest die Verfügbarkeit und bricht ab.
- Ungetestet gegen die echte Seite: die Feldnamen stammen aus Screenshots.
  Erst mit --once --debug laufen lassen und Ausgabe prüfen (siehe unten).

Einrichtung (auf Mac/PC/Raspberry Pi/Server, NICHT auf dem iPad):
    pip install playwright requests
    playwright install chromium
    cp config.example.json config.json   # und ausfüllen
    python traukalender_scanner.py --once --debug   # Probelauf, speichert Screenshots
    (auf einem Rechner mit Bildschirm zusätzlich --show für sichtbaren Browser)
    python traukalender_scanner.py                  # Dauerbetrieb

Push aufs iPad: App "ntfy" installieren, Thema (topic) aus config.json abonnieren.
"""

import argparse
import datetime as dt
import json
import random
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

START_URL = "https://traukalender.duesseldorf.de/de/Start-168.html"
MONTHS = {
    "Januar": 1, "Februar": 2, "März": 3, "April": 4, "Mai": 5, "Juni": 6,
    "Juli": 7, "August": 8, "September": 9, "Oktober": 10, "November": 11,
    "Dezember": 12,
}
MONTH_PATTERN = "|".join(MONTHS)
# Die Seite zeigt "Verfügbarkeit am Freitag, 5. Februar 2027" für den ersten freien Tag.
AVAIL_RE = re.compile(
    rf"Verfügbarkeit am\s+[A-Za-zäöüÄÖÜ]+,\s*(\d{{1,2}})\.\s*({MONTH_PATTERN})\s+(\d{{4}})"
)

FIELDS = [  # (Label auf der Seite, Schlüssel in config.json, Typ)
    ("Anrede", "anrede", "select"),
    ("Vorname", "vorname", "text"),
    ("Name", "name", "text"),
    ("Geburtsname (optional)", "geburtsname", "text"),
    ("Straße & Hausnummer", "strasse", "text"),
    ("PLZ", "plz", "text"),
    ("Ort", "ort", "text"),
    ("Land", "land", "select"),
    ("Staatsangehörigkeit", "staatsangehoerigkeit", "tags"),
    ("Geburtsdatum", "geburtsdatum", "text"),
    ("Geburtsort", "geburtsort", "text"),
    ("Geburtsland", "geburtsland", "select"),
    ("Familienstand", "familienstand", "select"),
]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(cfg: dict, title: str, message: str, urgent: bool = False) -> None:
    topic = cfg.get("ntfy_topic")
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,  # nur ASCII
                "Priority": "urgent" if urgent else "default",
                "Tags": "ring" if urgent else "information_source",
                "Click": START_URL,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        log(f"ntfy-Fehler: {e}")


def choose(page, loc, value: str) -> None:
    """Dropdown: erst native <select>, sonst Klick + Option anklicken."""
    try:
        loc.select_option(label=value, timeout=3000)
        return
    except Exception:
        pass
    loc.click()
    page.get_by_role("option", name=value, exact=True).first.click()


def fill_person(page, person: dict, idx: int) -> None:
    for label, key, kind in FIELDS:
        value = person.get(key, "")
        if not value:
            continue
        loc = page.get_by_label(label, exact=True).nth(idx)
        if kind == "text":
            loc.fill(value)
        elif kind == "select":
            choose(page, loc, value)
        elif kind == "tags":
            loc.click()
            page.keyboard.type(value)
            page.keyboard.press("Enter")


def first_free_date(page_text: str):
    dates = []
    for d, m, y in AVAIL_RE.findall(page_text):
        dates.append(dt.date(int(y), MONTHS[m], int(d)))
    return min(dates) if dates else None


def check_once(browser, cfg: dict, debug: bool):
    """Gibt (erstes_freies_Datum | None) zurück. Wirft bei Seitenfehlern."""
    ctx = browser.new_context(locale="de-DE", viewport={"width": 1280, "height": 1600})
    page = ctx.new_page()
    page.set_default_timeout(20000)
    try:
        page.goto(START_URL)
        # Schritt 1: Datenschutz
        page.get_by_role("checkbox").first.check()
        page.get_by_role("button", name="Weiter").first.click()
        # Schritt 2: Personendaten (wird NICHT abgeschickt, nur bis Terminübersicht)
        page.get_by_text("Person 2").first.wait_for()
        for i, person in enumerate(cfg["persons"]):
            fill_person(page, person, i)
        if debug:
            page.screenshot(path="debug_schritt2.png", full_page=True)
        page.get_by_role("button", name="Weiter").first.click()
        # Schritt 3: Termine
        page.get_by_text("Termine verfügbar").first.wait_for()
        time.sleep(1.5)  # Seite setzt den ersten freien Tag selbst
        text = page.inner_text("body")
        if debug:
            page.screenshot(path="debug_schritt3.png", full_page=True)
            Path("debug_schritt3.html").write_text(page.content(), encoding="utf-8")
        found = first_free_date(text)
        if found is None:
            raise RuntimeError(
                "Kein 'Verfügbarkeit am ...' gefunden - Seite anders als erwartet "
                "(mit --debug prüfen)."
            )
        return found
    finally:
        ctx.close()  # nie "Termin reservieren" klicken


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="nur ein Durchlauf")
    ap.add_argument("--debug", action="store_true", help="Screenshots/HTML speichern")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="nach so vielen Minuten sauber beenden (für GitHub Actions)")
    ap.add_argument("--announce", action="store_true",
                    help="beim Start eine 'Scanner läuft'-Nachricht schicken")
    ap.add_argument("--show", action="store_true", help="Browser sichtbar (nur mit Bildschirm)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    deadline = dt.date.fromisoformat(cfg.get("deadline", "2026-12-31"))
    interval = max(30, int(cfg.get("interval_seconds", 60)))
    last_alert = {"date": None, "ts": 0.0}
    errors = 0
    error_alerted = False

    started = time.time()
    log(f"Start. Stichtag {deadline}, Intervall {interval}s.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.show)
        first_ok = True
        while True:
            try:
                found = check_once(browser, cfg, args.debug)
                errors, error_alerted = 0, False
                log(f"Erster freier Termin laut Kalender: {found}")
                if first_ok and args.announce:
                    notify(cfg, "Scanner laeuft",
                           f"Erster freier Termin aktuell: {found:%d.%m.%Y}. "
                           f"Alarm bei Termin bis {deadline:%d.%m.%Y}.")
                first_ok = False
                if found <= deadline:
                    again = (last_alert["date"] != found
                             or time.time() - last_alert["ts"] > 900)
                    if again:
                        notify(cfg, "TRAUTERMIN FREI!",
                               f"Freier Termin am {found:%d.%m.%Y}. Sofort buchen!",
                               urgent=True)
                        last_alert = {"date": found, "ts": time.time()}
            except (PWTimeout, RuntimeError, Exception) as e:  # noqa: BLE001
                errors += 1
                # nur erste Zeile loggen: Playwright-Details könnten Ihre Eingaben enthalten
                first = (str(e).splitlines() or [""])[0][:200]
                log(f"Fehler ({errors}): {type(e).__name__}: {first}")
                if errors >= 5 and not error_alerted:
                    notify(cfg, "Scanner Problem",
                           "5 Fehler in Folge - Skript/Seite pruefen.")
                    error_alerted = True
            if args.once:
                break
            if args.max_minutes and time.time() - started + interval > args.max_minutes * 60:
                break
            time.sleep(interval + random.uniform(-5, 5))
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
