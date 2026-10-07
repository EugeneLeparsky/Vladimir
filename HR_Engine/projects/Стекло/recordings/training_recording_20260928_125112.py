import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\HR_Engine\\projects\\Стекло\\rabota_auth.json")
    page = context.new_page()
    page.goto("https://rabota.by/")
    page.get_by_role("link", name="Менеджер по работе с клиентами Минск до 4 октября").click()
    page.get_by_role("button", name="Соискатели").click()
    with page.expect_popup() as page1_info:
        page.get_by_role("link", name="Менеджер по работе с клиентами").nth(1).click()
    page1 = page1_info.value
    page1.get_by_role("button", name="Пригласить", exact=True).click()
    page1.get_by_role("combobox", name="Статус").click()
    page1.get_by_role("listbox").get_by_text("Подумать").click()
    page1.get_by_role("button", name="Изменить статус").click()
    page1.close()
    with page.expect_popup() as page2_info:
        page.get_by_role("link", name="Менеджер по работе с клиентами").nth(2).click()
    page2 = page2_info.value
    page2.get_by_role("button", name="Пригласить", exact=True).click()
    page2.get_by_role("combobox", name="Статус").click()
    page2.locator("div").filter(has_text="Изменить статус резюмеЛюбцова ЮлияЛюбцова ЮлияМенеджер по работе с клиентами (Ми").nth(2).click()
    page2.get_by_role("button", name="Изменить статус").click()
    page2.close()
    page.get_by_role("button", name="Женщина Специалист Бухта Марина Ивановна, 35 лет  •  Откликнулся 27").click()
    page.get_by_role("button", name="Женщина Специалист Бухта Марина Ивановна, 35 лет  •  Откликнулся 27").click()
    with page.expect_popup() as page3_info:
        page.get_by_role("link", name="Специалист").first.click()
    page3 = page3_info.value
    page3.get_by_role("button", name="Отказать", exact=True).click()
    page3.get_by_role("button", name="Не подходит").click()
    page3.get_by_role("button", name="Изменить статус").click()
    page3.get_by_role("button", name="Изменить статус").click()
    page3.get_by_text("Статус", exact=True).click()
    page3.get_by_text("Изменить статус резюмеБухта МаринаБухта МаринаМенеджер по работе с клиентами (Ми").click()
    page3.locator(".magritte-actions___r-a0l_1-0-31_xhh").click()
    page3.close()
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
