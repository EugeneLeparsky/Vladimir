"""Response identity is explicitly selected from an observed API schema."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qs, urlsplit

from .common import SetupError


def field(obj, path: str):
    value = obj
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise SetupError(f"В ответе rabota.by отсутствует поле {path}. Требуется повторная проверка схемы.")
        value = value[part]
    if not isinstance(value, (str, int, float, bool)) or value == "":
        raise SetupError(f"Поле {path} не содержит скалярного значения.")
    return value


def scalar_paths(obj, prefix=""):
    result = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if any(word in path.lower() for word in ("token", "cookie", "password", "secret", "authorization")):
                continue
            if isinstance(value, dict):
                result.update(scalar_paths(value, path))
            elif isinstance(value, (str, int, float, bool)) and value != "":
                result[path] = type(value).__name__
    return result


def received_at(value, encoding: str) -> str:
    try:
        if encoding in {"unix_seconds", "unix_milliseconds"}:
            if isinstance(value, bool):
                raise ValueError
            stamp = float(value) / (1000 if encoding == "unix_milliseconds" else 1)
            result = datetime.fromtimestamp(stamp, timezone.utc)
        elif encoding == "iso8601":
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise ValueError
        else:
            raise ValueError
        if result.year < 2000 or result.year > datetime.now(timezone.utc).year + 1:
            raise ValueError
        return result.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        raise SetupError("Дата отклика не соответствует выбранному формату/часовому поясу.") from None


def response_record(candidate: dict, vacancy: dict, schema: dict):
    if schema.get("confirmed") is not True:
        raise SetupError("Схема ID/даты/входящего отклика не подтверждена.")
    marker = schema.get("incoming_path")
    if marker:
        value = str(field(candidate, marker))
        if value not in schema.get("incoming_values", []):
            return None
    elif schema.get("incoming_only_confirmed") is not True:
        raise SetupError("Не подтверждено, что источник содержит только входящие отклики.")
    id_path = schema["response_id_path"]
    if id_path.split(".")[-1].lower() in {"resumeid", "vacancyid", "employerid", "applicantid", "candidateid"}:
        raise SetupError("ID резюме/кандидата/вакансии не является ID отдельного отклика.")
    response_id = field(candidate, id_path)
    resume_id = field(candidate, "resumeId")
    if isinstance(response_id, bool) or isinstance(resume_id, bool):
        raise SetupError("ID должен быть строкой или числом, не boolean.")
    # Tuple encoding has no separator collision. Pages/order/names never enter the key.
    key = json.dumps([str(vacancy["vacancy_id"]), str(response_id)], ensure_ascii=True, separators=(",", ":"))
    default_link = candidate.get("negotiationLinks", {}).get("changeTopic", {}).get("defaultLink", "")
    resume_hash = parse_qs(urlsplit(default_link).query).get("r", [None])[0]
    if not resume_hash or not re.fullmatch(r"[A-Za-z0-9_-]+", resume_hash):
        raise SetupError("В отклике нет проверенной ссылки на резюме (negotiationLinks/changeTopic/defaultLink/r).")
    return {
        "key": key, "response_id": str(response_id), "resume_id": str(resume_id),
        "vacancy_id": str(vacancy["vacancy_id"]), "vacancy_name": vacancy["name"],
        "vacancy_url": vacancy["url"],
        "received_at": received_at(field(candidate, schema["received_at_path"]), schema["time_encoding"]),
        "resume_url": f"https://rabota.by/resume/{resume_hash}?vacancyId={vacancy['vacancy_id']}",
    }
