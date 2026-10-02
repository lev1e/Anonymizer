"""Хранилище соответствий «токен ↔ исходное значение».

Один зашифрованный файл на компьютере пользователя. Токены выдаются на всё хранилище, а не на файл:
`Name7` в одном документе и `Name7` в другом — всегда один и тот же человек. Поэтому обезличенные
файлы можно свободно объединять в одном чате с моделью, а при восстановлении не нужно выбирать
ни ключ, ни «сессию»: токен сам однозначно указывает на значение.

Хранилище не содержит текста документов, только значения, которые были заменены.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import crypto
from .tokens import KIND_BY_CODE, Kind, format_token, token_base
from .util import atomic_write, opaque_id

DEFAULT_PREFS = {
    "retention_days": 180,     # 0 — хранить, пока пользователь сам не очистит
    "hide_terms": [],          # всегда скрывать
    "keep_terms": [],          # никогда не скрывать
    "numbers": False,          # заменять числа суррогатами (только Excel)
    "strict": False,           # скрывать и сомнительные слова: всё из списка «Возможно, нужно скрыть ещё»
    "countries": False,        # скрывать страны
    "neutral_names": True,     # называть результат «Файл1 (обезличено)» вместо исходного имени
}

MAX_HISTORY = 200


@dataclass(slots=True)
class Lookup:
    status: str                 # ok | unknown | variant_missing | removed
    original: str | None = None
    base: str = ""


def canon_number(text: str) -> str | None:
    """Каноническая запись числа: `1200`, `1200.0` и `1.2E3` — одно и то же число."""
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


class Vault:
    def __init__(self, path: Path | None = None, key: bytes | None = None) -> None:
        self.path = path
        self._key = key
        self._lock = threading.RLock()
        self.counters: dict[str, int] = {}
        self.entities: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.numbers: dict[str, str] = {}        # исходное → суррогат (канонические записи)
        self.numbers_back: dict[str, str] = {}   # суррогат → исходное
        self.file_names: dict[str, str] = {}     # номер нейтрального имени («Файл3» → "3») → исходное имя файла
        self.prefs: dict = json.loads(json.dumps(DEFAULT_PREFS))
        self.history: list[dict] = []
        # Идентификатор хранилища попадает в обезличенные файлы. По нему при возврате видно, что файл сделан другим
        # хранилищем: одинаковые метки (Name1) там означают других людей.
        self.vault_id = opaque_id(6).lower()
        self.known_ids: set[str] = {self.vault_id}
        self.notice = ""
        self.dirty = False
        if path is not None and path.exists():
            self._load()
        self._purge()

    # -- хранение -------------------------------------------------------------

    @classmethod
    def default(cls) -> "Vault":
        """Хранилище пользователя. Нечитаемое (другой компьютер, потерян ключ устройства) не удаляется, а
        откладывается в сторону: его можно вернуть вместе с ключом, а программа при этом запускается."""
        path = crypto.app_data_dir() / "vault.bin"
        try:
            return cls(path)
        except crypto.KeyErrorSafe as exc:
            aside = path.with_name(f"vault.unreadable.{int(time.time())}.bin")
            path.replace(aside)
            vault = cls(path)
            vault.notice = (f"Прежнее хранилище не удалось открыть ({exc}). Оно сохранено как {aside.name} в папке "
                            "программы. Файлы, обезличенные раньше, восстановить не получится, пока не будет возвращён "
                            "ключ устройства.")
            return vault

    def _payload(self) -> dict:
        return {"version": 1, "counters": self.counters, "entities": self.entities, "numbers": self.numbers,
                "file_names": self.file_names, "prefs": self.prefs, "history": self.history, "vault_id": self.vault_id, "known_ids": sorted(self.known_ids)}

    def _load(self) -> None:
        blob = self.path.read_bytes()
        try:
            data = json.loads(crypto.decrypt_blob(blob, self._key))
        except crypto.KeyErrorSafe:
            # Не затираем нечитаемое хранилище: пользователь может вернуть ключ устройства.
            raise
        self._apply(data)

    def _apply(self, data: dict) -> None:
        self.counters = {k: int(v) for k, v in data.get("counters", {}).items()}
        self.entities = data.get("entities", {})
        self.numbers = data.get("numbers", {})
        self.numbers_back = {v: k for k, v in self.numbers.items()}
        self.file_names = {str(k): v for k, v in data.get("file_names", {}).items()}
        self.prefs = {**json.loads(json.dumps(DEFAULT_PREFS)), **data.get("prefs", {})}
        self.history = data.get("history", [])
        self.vault_id = data.get("vault_id") or self.vault_id
        self.known_ids = set(data.get("known_ids", [])) | {self.vault_id}
        self.by_key = {self._key_of(e["kind"], e["key"]): base for base, e in self.entities.items()}

    def save(self) -> None:
        if self.path is None:
            self.dirty = False
            return
        with self._lock:
            raw = json.dumps(self._payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            blob = crypto.encrypt_blob(raw, self._key)
            if self.path.exists():
                try:
                    (self.path.with_suffix(".bak")).write_bytes(self.path.read_bytes())
                except OSError:
                    pass
            atomic_write(self.path, blob)
            self.dirty = False

    def commit(self) -> None:
        with self._lock:
            if self.dirty:
                self.save()

    @staticmethod
    def _key_of(kind: str, key: str) -> str:
        return f"{kind}\x00{key}"

    def _purge(self) -> None:
        days = int(self.prefs.get("retention_days") or 0)
        if days <= 0:
            return
        limit = time.time() - days * 86400
        stale = [base for base, e in self.entities.items() if e.get("used", 0) < limit]
        for base in stale:
            entity = self.entities.pop(base)
            self.by_key.pop(self._key_of(entity["kind"], entity["key"]), None)
        if stale:
            self.dirty = True

    def snapshot(self):
        """Состояние до задания: повторный проход с правками пользователя не должен оставлять пропусков в нумерации."""
        with self._lock:
            return copy.deepcopy((self.counters, self.entities, self.numbers, self.history, self.file_names))

    def rollback(self, snap) -> None:
        with self._lock:
            self.counters, self.entities, self.numbers, self.history, self.file_names = copy.deepcopy(snap)
            self.numbers_back = {v: k for k, v in self.numbers.items()}
            self.by_key = {self._key_of(e["kind"], e["key"]): base for base, e in self.entities.items()}
            self.dirty = True
            self.save()

    def clear(self) -> None:
        with self._lock:
            self.counters, self.entities, self.by_key = {}, {}, {}
            self.numbers, self.numbers_back, self.history = {}, {}, []
            self.file_names = {}
            # Нумерация начинается заново, поэтому файлы, обезличенные раньше, больше не относятся к этому хранилищу.
            self.vault_id = opaque_id(6).lower()
            self.known_ids = {self.vault_id}
            self.dirty = True
            self.save()

    # -- выдача токенов -------------------------------------------------------

    def token_for(self, kind_code: str, key: str, spelling: str | None, *, extension: str = "",
                  secret: bool = False) -> str:
        """Токен для написания `spelling` сущности (kind, key). Повторный вызов даёт тот же токен."""
        kind: Kind = KIND_BY_CODE[kind_code]
        with self._lock:
            composite = self._key_of(kind.code, key)
            base = self.by_key.get(composite)
            now = time.time()
            if base is None:
                number = self.counters.get(kind.stem, 0) + 1
                self.counters[kind.stem] = number
                base = token_base(kind, number)
                self.entities[base] = {"kind": kind.code, "key": key, "spellings": [], "created": now,
                                       "used": now, "secret": secret}
                self.by_key[composite] = base
                self.dirty = True
            entity = self.entities[base]
            entity["used"] = now
            if secret or spelling is None:
                return format_token(kind, _number_of(base), 1, extension)
            spellings: list[str] = entity["spellings"]
            if spelling in spellings:
                variant = spellings.index(spelling) + 1
            else:
                spellings.append(spelling)
                variant = len(spellings)
                self.dirty = True
            return format_token(kind, _number_of(base), variant, extension)

    def lookup(self, kind: Kind, number: int, variant: int = 1) -> Lookup:
        with self._lock:
            base = token_base(kind, number)
            entity = self.entities.get(base)
            if entity is None:
                return Lookup("unknown", None, base)
            if entity.get("secret"):
                return Lookup("removed", None, base)
            spellings = entity["spellings"]
            if not spellings:
                return Lookup("removed", None, base)
            if variant > len(spellings):
                return Lookup("variant_missing", spellings[0], base)
            entity["used"] = time.time()
            self.dirty = True
            return Lookup("ok", spellings[variant - 1], base)

    def entity_label(self, base: str) -> str:
        entity = self.entities.get(base)
        return entity["spellings"][0] if entity and entity.get("spellings") else ""

    # -- числовые суррогаты ---------------------------------------------------

    def surrogate_for(self, original: str, make) -> str:
        """Суррогат числа: одно и то же число всегда даёт один и тот же суррогат."""
        with self._lock:
            found = self.numbers.get(original)
            if found is not None:
                return found
            for _ in range(200):
                candidate = make()
                if candidate not in self.numbers_back and candidate != original:
                    break
            else:  # практически недостижимо: пространство суррогатов огромно
                raise RuntimeError("Не удалось подобрать суррогат числа")
            self.numbers[original] = candidate
            self.numbers_back[candidate] = original
            self.dirty = True
            return candidate

    def original_of_surrogate(self, surrogate: str) -> str | None:
        return self.numbers_back.get(surrogate)

    # -- нейтральные имена файлов ---------------------------------------------

    def file_number(self, original: str) -> int:
        """Номер нейтрального имени для исходного имени файла: одно и то же имя всегда получает один номер."""
        with self._lock:
            for number, name in self.file_names.items():
                if name == original:
                    return int(number)
            number = max((int(n) for n in self.file_names), default=0) + 1
            self.file_names[str(number)] = original
            self.dirty = True
            return number

    def original_file_name(self, number: int) -> str | None:
        return self.file_names.get(str(number))

    # -- настройки и журнал ---------------------------------------------------

    def set_prefs(self, **values) -> None:
        with self._lock:
            for name, value in values.items():
                if name in DEFAULT_PREFS:
                    self.prefs[name] = value
            self.dirty = True
            self.save()

    def log(self, kind: str, files: list[str], counts: dict[str, int], extra: dict | None = None) -> None:
        with self._lock:
            self.history.append({"id": opaque_id(5), "time": time.time(), "kind": kind, "files": files,
                                 "counts": counts, **(extra or {})})
            del self.history[:-MAX_HISTORY]
            self.dirty = True

    def stats(self) -> dict:
        with self._lock:
            by_kind: dict[str, int] = {}
            for entity in self.entities.values():
                by_kind[entity["kind"]] = by_kind.get(entity["kind"], 0) + 1
            return {"entities": len(self.entities), "numbers": len(self.numbers), "by_kind": by_kind}

    # -- резервная копия ------------------------------------------------------

    def export_backup(self, password: str) -> bytes:
        with self._lock:
            raw = json.dumps(self._payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return crypto.password_encrypt(raw, password)

    def import_backup(self, blob: bytes, password: str) -> dict:
        """Слияние возможно, только если токены обоих хранилищ не спорят: иначе `Name5` из копии
        означал бы другого человека, чем `Name5` здесь, и файлы обоих компьютеров перепутались бы."""
        data = json.loads(crypto.password_decrypt(blob, password))
        with self._lock:
            conflicts = 0
            for base, entity in data.get("entities", {}).items():
                local = self.entities.get(base)
                if local and (local["kind"], local["key"]) != (entity["kind"], entity["key"]):
                    conflicts += 1
            if conflicts and self.entities:
                raise ValueError(
                    f"Импорт невозможен: в {conflicts} случаях один и тот же токен в копии и на этом компьютере "
                    "означает разные значения. Импортируйте копию в пустое хранилище (очистите историю).")
            if not self.entities:
                self._apply(data)
            else:
                for base, entity in data.get("entities", {}).items():
                    local = self.entities.get(base)
                    if local is None:
                        self.entities[base] = entity
                    else:
                        for spelling in entity.get("spellings", []):
                            if spelling not in local["spellings"]:
                                local["spellings"].append(spelling)
                for stem, value in data.get("counters", {}).items():
                    self.counters[stem] = max(self.counters.get(stem, 0), int(value))
                self.known_ids |= set(data.get("known_ids", [])) | ({data["vault_id"]} if data.get("vault_id") else set())
                for original, surrogate in data.get("numbers", {}).items():
                    self.numbers.setdefault(original, surrogate)
                self.numbers_back = {v: k for k, v in self.numbers.items()}
                for number, name in data.get("file_names", {}).items():
                    self.file_names.setdefault(str(number), name)
                self.by_key = {self._key_of(e["kind"], e["key"]): b for b, e in self.entities.items()}
            self.dirty = True
            self.save()
            return {"entities": len(self.entities)}


def _number_of(base: str) -> int:
    match = re.search(r"(\d+)", base)
    return int(match.group(1))
