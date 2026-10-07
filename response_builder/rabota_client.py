from __future__ import annotations

import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.sync_api import sync_playwright

from .common import SetupError, atomic_json, file_lock


BASE = "https://rabota.by"
VACANCIES = BASE + "/employer/vacancies"
RESPONSES = BASE + "/employer/vacancyresponses"
API = BASE + "/shards/employer/vacancyresponses/candidates_list"


def vacancy_id_from_url(url):
    parts = urlsplit(url)
    if parts.hostname not in {"rabota.by", "www.rabota.by"}:
        return None
    match = re.fullmatch(r"/vacancy/(\d+)/?", parts.path)
    return match.group(1) if match else None


class Rabota:
    def __init__(self, shared: Path, visible=False, interactive=False):
        self.shared = shared
        self.visible = visible
        self.interactive = interactive
        self.auth = shared / "rabota_auth.json"
        self.playwright = None
        self.browser = None
        self.context = None

    def __enter__(self):
        try:
            self.playwright = sync_playwright().start()
            self._launch(self.visible)
            if not self.auth.is_file():
                self._login()
            else:
                try:
                    self._employer_page()
                except SessionExpired:
                    self._login()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _launch(self, visible, fresh=False):
        if self.browser:
            self.browser.close()
        options = {"headless": not visible}
        if os.environ.get("RESPONSES_BROWSER_EXECUTABLE"):
            options["executable_path"] = os.environ["RESPONSES_BROWSER_EXECUTABLE"]
        self.browser = self.playwright.chromium.launch(**options)
        options = {"storage_state": str(self.auth)} if self.auth.exists() and not fresh else {}
        try:
            self.context = self.browser.new_context(**options)
        except Exception:
            if not self.interactive:
                raise SetupError("Не читается общая сессия rabota.by. Запустите проверку авторизации.") from None
            self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def _employer_page(self):
        result = self.page.goto(VACANCIES, wait_until="domcontentloaded", timeout=60000)
        if result is None:
            raise SetupError("rabota.by не вернул ответ страницы работодателя.")
        if result.status == 401 or "/account/login" in self.page.url:
            raise SessionExpired("Сессия работодателя истекла.")
        if result.status >= 400:
            raise SetupError(f"Кабинет rabota.by недоступен: HTTP {result.status}. Проверьте сеть и права.")
        if urlsplit(self.page.url).path != "/employer/vacancies":
            raise SessionExpired("Не удалось войти в кабинет работодателя.")
        self.page.wait_for_timeout(1500)
        if self.page.locator('input[type="password"]').count():
            raise SessionExpired("rabota.by требует вход.")

    def _login(self):
        if not self.interactive:
            raise SetupError("Нет рабочей сессии rabota.by. Выберите «Проверка Google + rabota.by» для ручного входа.")
        with file_lock(self.shared / ".rabota-auth.lock"):
            self._launch(True, fresh=True)
            self.page.goto(BASE + "/account/login?backurl=%2Femployer%2Fvacancies",
                           wait_until="domcontentloaded", timeout=60000)
            print("Войдите в аккаунт РАБОТОДАТЕЛЯ в открытом браузере. Не закрывайте окно.")
            input("После входа нажмите Enter здесь: ")
            self._employer_page()
            if not self.context.cookies(BASE):
                raise SetupError("Вход не подтверждён: нет cookies rabota.by.")
            atomic_json(self.auth, self.context.storage_state())
            if not self.visible:
                self._launch(False)
                self._employer_page()

    def vacancies(self):
        self._employer_page()
        result, visited = {}, set()
        for _ in range(1000):
            if self.page.url in visited:
                raise SetupError("Повтор страницы вакансий: полный список не подтверждён.")
            visited.add(self.page.url)
            links = self.page.locator('a[href*="/vacancy/"]').evaluate_all(
                "nodes => nodes.map(a => ({url:a.href,name:a.innerText.trim()}))")
            for link in links:
                identifier = vacancy_id_from_url(link["url"])
                if identifier and link["name"] and identifier not in result:
                    result[identifier] = {"vacancy_id": identifier, "name": link["name"],
                                          "url": BASE + "/vacancy/" + identifier}
            next_page = self.page.locator('[data-qa="pager-next"]').first
            if not next_page.count() or next_page.get_attribute("aria-disabled") == "true":
                break
            href = next_page.get_attribute("href")
            if not href:
                raise SetupError("Не удалось определить следующую страницу вакансий.")
            url = urljoin(self.page.url, href)
            parts = urlsplit(url)
            if parts.hostname != "rabota.by" or not parts.path.startswith("/employer/"):
                raise SetupError("Неожиданная ссылка пагинации работодателя.")
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(800)
        else:
            raise SetupError("Достигнут предел страниц вакансий; список не подтверждён.")
        if not result:
            raise SetupError("Не найдены опубликованные вакансии. Возможно, изменился интерфейс кабинета.")
        return list(result.values())

    def candidates(self, vacancy_id):
        if not str(vacancy_id).isdigit():
            raise SetupError("Некорректный vacancy_id.")
        previous_pages = set()
        for page in range(10000):
            response = self.context.request.get(API, params={"vacancyId": str(vacancy_id),
                "order": "RELEVANCE", "limit": 50, "page": page},
                headers={"Accept": "application/json", "Referer": RESPONSES + "?vacancyId=" + str(vacancy_id)},
                timeout=60000)
            if response.status == 401:
                raise SessionExpired("Сессия rabota.by истекла во время сбора. Выполните проверку авторизации.")
            if not response.ok:
                raise SetupError(f"Список откликов недоступен: HTTP {response.status}.")
            try:
                data = response.json()
                candidates = data["contactCenterCandidates"]["candidates"]
            except (ValueError, KeyError, TypeError):
                raise SetupError("Изменилась структура candidates_list; требуется диагностика схемы.") from None
            if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
                raise SetupError("Некорректный список откликов rabota.by.")
            if not candidates:
                return
            # Detect a ignored page parameter without using a page position as a checkpoint.
            import hashlib, json
            signature = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).digest()
            if signature in previous_pages:
                raise SetupError("API повторяет страницу откликов. Полнота прохода не подтверждена.")
            previous_pages.add(signature)
            yield from candidates
        raise SetupError("Достигнут предел страниц откликов; проход не завершён.")

    def extract(self, record):
        result = dict(record)
        self.page.goto(record["resume_url"], wait_until="domcontentloaded", timeout=60000)
        if "/account/login" in self.page.url:
            raise SessionExpired("При чтении резюме rabota.by требует вход.")
        name = self.page.locator('[data-qa="resume-main-info__header"]').first
        try:
            name.wait_for(state="visible", timeout=20000)
        except Exception:
            raise SetupError("Не найден блок резюме. Запись оставлена для повтора.") from None
        result["name"] = name.inner_text().strip()
        if not result["name"]:
            raise SetupError("Не удалось прочитать имя кандидата.")
        contacts = self.page.locator("a").filter(has_text="Показать все контакты")
        if contacts.count():
            contacts.first.click(timeout=5000)
            self.page.wait_for_timeout(1500)
        text = self.page.locator("body").inner_text()
        age = re.search(r"(?<!\d)(\d{1,2})\s+(?:лет|года|год)\b", text, re.I)
        result["age"] = age.group(1) if age else ""
        phones = self.page.locator('a[href^="tel:"]').evaluate_all("xs => xs.map(x => x.getAttribute('href').slice(4))")
        emails = self.page.locator('a[href^="mailto:"]').evaluate_all("xs => xs.map(x => x.getAttribute('href').slice(7).split('?')[0])")
        result["phone"] = "; ".join(dict.fromkeys(phones))
        result["email"] = "; ".join(dict.fromkeys(emails))
        # Missing/closed contact details are legitimate; an unreadable resume is an error.
        result["resume_text"] = text[:45000]
        return result

    def __exit__(self, *_):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.playwright:
                self.playwright.stop()


class SessionExpired(SetupError):
    pass
