"""Captures report screenshots by driving the running app through one full patient journey (real model calls).

Dev-only tool (not needed to run the app):  pip install playwright   (uses the Microsoft Edge already on Windows)
Start the app on FRESH demo data first (rename data/careflow.db, then python app.py), then:

    python docs/make_screenshots.py
"""
from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8050"
OUT = Path(__file__).with_name("screenshots")
OUT.mkdir(exist_ok=True)
AI_TIMEOUT = 120_000


def shot(page, name, selector=None, full=False):
    path = OUT / f"{name}.png"
    if selector:
        page.locator(selector).first.screenshot(path=str(path))
    else:
        page.screenshot(path=str(path), full_page=full)
    print("saved", path.name)


def login(page, user, pw):
    page.goto(f"{BASE}/login")
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", pw)
    page.click("form[action='/login'] button")
    page.wait_for_load_state()


def pid_of(page, name) -> int:
    page.goto(BASE + "/")
    href = page.locator(f"a.pc-main:has-text('{name}')").first.get_attribute("href")
    return int(href.rsplit("/", 1)[1])


def submit(page, selector, timeout=30_000):
    with page.expect_navigation(timeout=timeout):
        page.locator(selector).first.click()


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge")
        ctx = browser.new_context(viewport={"width": 1366, "height": 860}, device_scale_factor=1.5)
        page = ctx.new_page()

        # --- login + lockout evidence
        page.goto(f"{BASE}/login")
        page.fill("input[name=username]", "dr.mehta")
        shot(page, "01_login")
        for _ in range(5):
            page.goto(f"{BASE}/login")
            page.fill("input[name=username]", "sister.anita")
            page.fill("input[name=password]", "wrong-guess-1")
            page.click("form[action='/login'] button")
        shot(page, "02_lockout", ".login-wrap")

        login(page, "dr.mehta", "mehta-demo-2026")
        page.goto(BASE + "/")
        shot(page, "03_dashboard", full=True)
        meera, priya, sunita, kavita = (pid_of(page, n) for n in ("Meera", "Priya", "Sunita", "Kavita"))

        # --- admission papers: read with AI, verify, complete
        page.goto(f"{BASE}/patient/{meera}#admission")
        submit(page, f"form[action='/patient/{meera}/sample'] button")
        submit(page, f"form[action='/patient/{meera}/extract'] button", AI_TIMEOUT)
        shot(page, "04_ai_reading_to_verify", "table.verify")
        name_form = page.locator("form.vform:has(input[value='patient.name'])")
        if name_form.count():
            val = page.locator("table.verify tr:has(input[value='patient.name']) td").nth(1).inner_text()
            if "meera" not in val.lower():
                name_form.locator("input[name=corrected]").fill("Meera Sharma")
                with page.expect_navigation():
                    name_form.locator("button[value=corrected]").click()
        while page.locator("table.verify tr.must form.vform").count():
            with page.expect_navigation():
                page.locator("table.verify tr.must form.vform button[value=confirmed]").first.click()
        page.locator("form.checklist").scroll_into_view_if_needed()
        shot(page, "05_admission_checklist", "form.checklist")
        f = page.locator("form.checklist")
        f.locator("input[name=allergies]").fill("Penicillin")
        f.locator("select[name=blood_group]").select_option("B+")
        f.locator("input[name=gravida_para]").fill("G2P1L1")
        f.locator("input[name=gestational_age]").fill("37+2 weeks")
        f.locator("input[name=edd]").fill("10/11/26")
        f.locator("input[name=admitting_diagnosis]").fill("G2P1L1 at 37+2 weeks, gestational hypertension, ? pre-eclampsia")
        with page.expect_navigation():
            f.locator("button").click()
        page.evaluate("window.scrollTo(0, 0)")
        shot(page, "06_admitted_with_caveat")

        # --- allergy block
        mf = page.locator(f"form[action='/patient/{meera}/event']:has(input[value=med_admin])")
        mf.locator("select[name=item_code]").select_option("AMPI_INJ_500")
        mf.locator("input[name=dose]").fill("500 mg")
        with page.expect_navigation():
            mf.locator("button").click()
        page.evaluate("window.scrollTo(0, 0)")
        shot(page, "07_allergy_block")

        # --- injection page on Meera's record
        page.goto(f"{BASE}/patient/{meera}#admission")
        page.locator(f"form[action='/patient/{meera}/sample'] select[name=which]").select_option("injection_test_nursing_note.png")
        submit(page, f"form[action='/patient/{meera}/sample'] button")
        submit(page, f"form[action='/patient/{meera}/extract'] button", AI_TIMEOUT)
        page.locator(".danger-box").first.scroll_into_view_if_needed()
        shot(page, "08_injection_detected", ".danger-box")

        # --- labour: add a check, partograph, then delivery
        page.goto(f"{BASE}/patient/{priya}#labour")
        lf = page.locator(f"form[action='/patient/{priya}/event']:has(input[value=labour])")
        for k, v in {"dilatation_cm": "8", "descent_fifths": "2", "contractions_10min": "4", "contraction_secs": "45", "fhr": "150",
                     "sbp": "124", "dbp": "80", "hr": "90"}.items():
            lf.locator(f"input[name={k}]").fill(v)
        lf.locator("select[name=liquor]").select_option("clear")
        lf.locator("select[name=moulding]").select_option("+")
        with page.expect_navigation():
            lf.locator("button").click()
        shot(page, "09_labour_partograph", "#labour")
        df = page.locator("#deliver")
        df.locator("select[name=mode]").select_option("NVD")
        df.locator("select[name=sex]").select_option("F")
        for k, v in {"weight_g": "3050", "apgar1": "8", "apgar5": "9", "ebl_ml": "350"}.items():
            df.locator(f"input[name={k}]").fill(v)
        df.locator("select[name=perineum]").select_option("first")
        with page.expect_navigation():
            df.locator("button").click()
        shot(page, "10_baby_record_created")
        page.locator("#vaccines").scroll_into_view_if_needed()
        shot(page, "11_vaccines_iap", "#vaccines")

        # --- chatbot
        page.goto(BASE + "/assistant")
        page.fill("#chat-q", "Which babies still need their Hepatitis B birth dose, and which mothers have pending alerts?")
        page.keyboard.press("Enter")
        page.wait_for_selector(".bubble.bot:not(.thinking) .trace", timeout=AI_TIMEOUT)
        page.wait_for_function("!document.querySelector('.bubble.thinking')", timeout=AI_TIMEOUT)
        shot(page, "12_chatbot", "#chat")

        # --- alerts
        page.goto(BASE + "/alerts")
        shot(page, "13_alerts")

        # --- handover draft
        page.goto(f"{BASE}/patient/{sunita}#docs")
        submit(page, f"form[action='/patient/{sunita}/draft']:has(input[value=handover]) button", AI_TIMEOUT)
        shot(page, "14_handover_draft", ".draft")

        # --- discharge Kavita + baby
        page.goto(f"{BASE}/patient/{kavita}#docs")
        submit(page, f"form[action='/patient/{kavita}/draft']:has(input[value=discharge]) button", AI_TIMEOUT)
        shot(page, "15_discharge_draft", ".draft")
        with page.expect_navigation():
            page.locator("form[action$='/approve'] button").first.click()
        baby = page.locator("a:has-text('Baby girl of Kavita')").first.get_attribute("href")
        bid = int(baby.rsplit("/", 1)[1])
        page.goto(f"{BASE}/patient/{bid}#docs")
        submit(page, f"form[action='/patient/{bid}/draft']:has(input[value=baby_discharge]) button", AI_TIMEOUT)
        with page.expect_navigation():
            page.locator("form[action$='/approve'] button").first.click()
        page.goto(f"{BASE}/patient/{kavita}#docs")
        page.locator("form[action$='/discharge']").scroll_into_view_if_needed()
        shot(page, "16_discharge_checks", "form[action$='/discharge']")
        page.on("dialog", lambda d: d.accept())
        with page.expect_navigation():
            page.locator("form[action$='/discharge'] button").click()
        page.goto(f"{BASE}/bill/{kavita}")
        shot(page, "17_final_bill", full=True)

        # --- reminders
        page.goto(BASE + "/reminders")
        page.locator("details").first.evaluate("d => d.open = true")
        shot(page, "18_reminders", full=True)
        with page.expect_navigation():
            page.locator("form[action$='/authorise'] button").first.click()
        with page.expect_navigation():
            page.locator("form[action$='/send'] button").first.click()
        page.evaluate("window.scrollTo(0, 0)")
        shot(page, "19_reminder_sent")

        # --- usage + rules
        page.goto(BASE + "/usage")
        shot(page, "20_usage", full=True)
        page.goto(BASE + "/rules")
        shot(page, "21_rules", full=True)

        # --- admin
        page.goto(BASE + "/login")
        ctx2 = browser.new_context(viewport={"width": 1366, "height": 860}, device_scale_factor=1.5)
        p2 = ctx2.new_page()
        login(p2, "admin", "admin-demo-2026")
        p2.goto(BASE + "/admin/users")
        shot(p2, "22_users_admin", full=True)
        browser.close()


if __name__ == "__main__":
    main()
