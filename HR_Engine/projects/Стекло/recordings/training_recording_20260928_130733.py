import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\HR_Engine\\projects\\Стекло\\rabota_auth.json")
    page = context.new_page()
    page.goto("https://rabota.by/")
    page.get_by_role("button", name="Отклики +21").click()
    page.get_by_role("link", name="Не подходит").click()
    with page.expect_popup() as page1_info:
        page.get_by_role("link", name="Специалист", exact=True).click()
    page1 = page1_info.value
    page1.close()
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
