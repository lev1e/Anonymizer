"""Запуск интерфейса для разработки: фиксированный порт, без автоматического открытия браузера и без таймаута простоя."""
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anonymizer import webapp  # noqa: E402
from anonymizer.vault import Vault  # noqa: E402
from anonymizer.webapp import create_server  # noqa: E402

# Только для разработки: отдаёт файлы из папки case, чтобы проверять интерфейс без выбора файла вручную.
_original_get = webapp.Handler.do_GET


def _get(self):
    if self.path.startswith("/dev-fixture/") and self._authorized():
        import urllib.parse
        target = Path(__file__).resolve().parent.parent / "case" / urllib.parse.unquote(self.path.split("/dev-fixture/", 1)[1])
        if target.is_file():
            return self._send(200, target.read_bytes(), "application/octet-stream")
        return self._error(404, "no fixture")
    return _original_get(self)


webapp.Handler.do_GET = _get

home = Path(os.environ.get("ANONYMIZER_HOME") or tempfile.mkdtemp(prefix="anonymizer-dev-"))
os.environ["ANONYMIZER_HOME"] = str(home)
server, app = create_server(Vault(home / "vault.bin"), home / "work", port=int(os.environ.get("PORT", "8765")))
app.token = "devtoken"
print(f"http://127.0.0.1:{server.server_address[1]}/?t={app.token}", flush=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
app.stop.wait()
