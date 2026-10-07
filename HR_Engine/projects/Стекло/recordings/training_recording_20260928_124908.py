import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\HR_Engine\\projects\\Стекло\\rabota_auth.json")
    page = context.new_page()
    page.goto("https://rabota.by/")
    page1 = context.new_page()
    page1.goto("https://docs.google.com/spreadsheets/d/1NW7xoNjeSew1s9xl5BHJJIJGPiM_9oXI0r87JFNKdiM/edit?gid=1389520146#gid=1389520146")
    page1.locator("[id=\"1389520146-scrollable\"] > div:nth-child(2)").click()
    page1.locator("[id=\"1389520146-scrollable\"] > div:nth-child(2)").click()
    page1.locator("[id=\"1389520146-scrollable\"] > div:nth-child(2)").click()
    with page1.expect_popup() as page2_info:
        page1.get_by_role("link", name="https://rabota.by/resume/").click()
    page2 = page2_info.value
    page2.goto("https://rabota.by/resume/26b9a1ed0008284fab009818cb375975733650?vacancyId=136993748")
    page2.close()
    page1.close()
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
