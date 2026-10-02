"""Секреты в каждом укромном месте файла: ни один не должен пережить обезличивание, и все должны вернуться."""
import re
import unittest

from . import leak_probe as L
from .helpers import Env

TOKEN = re.compile(r"\b(?:Name|Company|City|Phone|Meta|Domain|Data)\d+(?:_\d+)?\b|email\d+(?:_\d+)?@example\.com")


class Base(unittest.TestCase):
    build = None
    name = ""

    def setUp(self):
        self.env = Env()
        self.data = type(self).build()

    def tearDown(self):
        self.env.close()

    def test_nothing_survives_anywhere_in_the_file(self):
        job = self.env.anonymize((self.name, self.data))
        self.assertIn(job.files[0].status, ("ok", "attention"), job.files[0].messages)
        hidden = self.env.output_bytes(job)
        self.assertEqual(L.leaks(self.data) != [], True, "проба должна содержать секреты")
        self.assertEqual(L.leaks(hidden), [])

    def test_everything_comes_back_exactly(self):
        job = self.env.anonymize((self.name, self.data))
        back = self.env.restore(self.env.as_upload(job))
        restored = self.env.output_bytes(back)
        original = L.everything(self.data)
        after = L.everything(restored)
        for secret in L.SECRETS:
            lost = [p for p, t in original.items() if secret.lower() in t.lower() and secret.lower() not in after.get(p, "").lower()
                    and not p.startswith("docProps/thumbnail")]
            self.assertEqual(lost, [], f"{secret!r} не вернулся в: {lost}")
        for part, text in after.items():
            self.assertIsNone(TOKEN.search(text), f"остался токен в {part}: {TOKEN.search(text) and TOKEN.search(text).group(0)}")


class Word(Base):
    build, name = staticmethod(L.rich_docx), "rich.docx"


class Excel(Base):
    build, name = staticmethod(L.rich_xlsx), "rich.xlsx"


class PowerPoint(Base):
    build, name = staticmethod(L.rich_pptx), "rich.pptx"


del Base

if __name__ == "__main__":
    unittest.main()
