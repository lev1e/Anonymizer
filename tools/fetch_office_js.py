"""Скачивает Office.js (Microsoft) в anonymizer/addin_web/vendor: панель работает с локальной копией, без сети.

Файлы Microsoft в репозиторий не входят: лицензия Office.js разрешает распространять их только вместе с приложением и с
условиями для распространителя. Скрипт запускают при сборке и в разработке. Запуск: python tools/fetch_office_js.py
"""
import io
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "1.1.110"
URL = f"https://registry.npmjs.org/@microsoft/office-js/-/office-js-{VERSION}.tgz"
DEST = Path(__file__).resolve().parent.parent / "anonymizer" / "addin_web" / "vendor"
HOSTS = ("word-15", "word-win32", "word-mac", "excel-15", "excel-win32", "excel-mac", "excelwebapp-15",
         "powerpoint-15", "powerpoint-win32", "powerpoint-mac")
ROOT_FILES = {"office.js", "es6-promise.js", "o15apptofilemappingtable.js"}


def wanted(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    if name.startswith("package/dist/") and base.endswith(".js") and not base.endswith(".debug.js"):
        if "/" not in name[len("package/dist/"):]:
            return base in ROOT_FILES or base.startswith(HOSTS)
        return name.endswith(("en-us/office_strings.js", "ru-ru/office_strings.js"))
    return name == "package/LICENSE.md"


def main() -> int:
    if (DEST / "office.js").is_file() and "--force" not in sys.argv:
        print("Office.js уже на месте:", DEST)
        return 0
    print("Загрузка", URL)
    data = urllib.request.urlopen(URL, timeout=120).read()      # noqa: S310 (адрес зафиксирован выше)
    shutil.rmtree(DEST, ignore_errors=True)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            if member.isfile() and wanted(member.name):
                relative = member.name[len("package/dist/"):] if member.name.startswith("package/dist/") else "LICENSE-office-js.md"
                target = DEST / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                count += 1
    print(f"Готово: {count} файлов в {DEST}")
    return 0 if (DEST / "office.js").is_file() else 1


if __name__ == "__main__":
    raise SystemExit(main())
