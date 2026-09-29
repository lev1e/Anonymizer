import os
import tempfile
import time
import unittest
from pathlib import Path

from anonymizer import crypto
from anonymizer.tokens import KIND_BY_CODE
from anonymizer.vault import Vault, canon_number


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "vault.bin"
        self.key = os.urandom(32)
        self.vault = Vault(self.path, key=self.key)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_entity_always_gets_the_same_token(self):
        first = self.vault.token_for("PERSON", "иванов иван", "Иванов Иван")
        again = self.vault.token_for("PERSON", "иванов иван", "Иванов Иван")
        self.assertEqual(first, again)
        self.assertEqual(first, "Name1")

    def test_numbering_is_per_kind_and_in_order_of_appearance(self):
        tokens = [self.vault.token_for("PERSON", "a", "A"), self.vault.token_for("ORG", "o", "O"),
                  self.vault.token_for("PERSON", "b", "B"), self.vault.token_for("CITY", "c", "C")]
        self.assertEqual(tokens, ["Name1", "Company1", "Name2", "City1"])

    def test_other_spellings_of_one_entity_are_variants(self):
        base = self.vault.token_for("PERSON", "k", "Иванов Иван")
        second = self.vault.token_for("PERSON", "k", "Иванову И.")
        third = self.vault.token_for("PERSON", "k", "И. Иванов")
        self.assertEqual((base, second, third), ("Name1", "Name1_2", "Name1_3"))
        self.assertEqual(self.vault.lookup(KIND_BY_CODE["PERSON"], 1, 2).original, "Иванову И.")
        self.assertEqual(self.vault.lookup(KIND_BY_CODE["PERSON"], 1, 1).original, "Иванов Иван")

    def test_lookup_reports_what_is_wrong(self):
        self.vault.token_for("PERSON", "k", "Иванов")
        person = KIND_BY_CODE["PERSON"]
        self.assertEqual(self.vault.lookup(person, 77).status, "unknown")
        self.assertEqual(self.vault.lookup(person, 1, 9).status, "variant_missing")
        self.assertEqual(self.vault.lookup(person, 1).status, "ok")

    def test_secret_is_never_stored(self):
        token = self.vault.token_for("SECRET", "hash", None, secret=True)
        self.assertEqual(self.vault.lookup(KIND_BY_CODE["SECRET"], 1).status, "removed")
        self.vault.save()
        self.assertNotIn(b"password", self.path.read_bytes())
        self.assertTrue(token.startswith("Secret"))

    def test_state_survives_a_restart_and_is_encrypted_on_disk(self):
        self.vault.token_for("PERSON", "k", "Секретный Сотрудник")
        self.vault.save()
        raw = self.path.read_bytes()
        self.assertNotIn("Секретный".encode("utf-8"), raw)
        self.assertNotIn(b"Name1", raw)
        again = Vault(self.path, key=self.key)
        self.assertEqual(again.token_for("PERSON", "k", "Секретный Сотрудник"), "Name1")
        self.assertEqual(again.token_for("PERSON", "other", "Другой"), "Name2")

    def test_a_foreign_device_key_is_refused_without_destroying_the_vault(self):
        self.vault.token_for("PERSON", "k", "Иванов")
        self.vault.save()
        before = self.path.read_bytes()
        with self.assertRaises(crypto.KeyErrorSafe):
            Vault(self.path, key=os.urandom(32))
        self.assertEqual(self.path.read_bytes(), before)

    def test_unreadable_default_vault_is_set_aside_not_destroyed(self):
        import os as _os
        from unittest import mock
        self.vault.token_for("PERSON", "k", "Иванов")
        self.vault.save()
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        (home / "vault.bin").write_bytes(self.path.read_bytes())     # зашифровано другим ключом
        with mock.patch.dict(_os.environ, {"ANONYMIZER_HOME": str(home)}):
            with mock.patch.object(crypto, "load_device_key", return_value=_os.urandom(32)):
                vault = Vault.default()
        self.assertIn("не удалось открыть", vault.notice)
        self.assertEqual(vault.stats()["entities"], 0)
        self.assertEqual(len(list(home.glob("vault.unreadable.*.bin"))), 1)

    def test_rollback_returns_numbers_issued_for_nothing(self):
        snapshot = self.vault.snapshot()
        self.vault.token_for("PERSON", "x", "X")
        self.vault.rollback(snapshot)
        self.assertEqual(self.vault.token_for("PERSON", "y", "Y"), "Name1")

    def test_entities_unused_for_too_long_are_forgotten(self):
        self.vault.token_for("PERSON", "old", "Старый")
        self.vault.entities["Name1"]["used"] = time.time() - 400 * 86400
        self.vault.prefs["retention_days"] = 180
        self.vault.save()
        self.assertEqual(Vault(self.path, key=self.key).stats()["entities"], 0)

    def test_retention_zero_keeps_everything(self):
        self.vault.token_for("PERSON", "old", "Старый")
        self.vault.entities["Name1"]["used"] = time.time() - 4000 * 86400
        self.vault.prefs["retention_days"] = 0
        self.vault.save()
        self.assertEqual(Vault(self.path, key=self.key).stats()["entities"], 1)

    def test_numeric_surrogate_is_stable_and_reversible(self):
        made = iter(["1187", "1187", "1190"])
        first = self.vault.surrogate_for("1200", lambda: next(made))
        again = self.vault.surrogate_for("1200", lambda: next(made))
        self.assertEqual(first, again)
        self.assertEqual(self.vault.original_of_surrogate(first), "1200")

    def test_numbers_are_canonical(self):
        self.assertEqual(canon_number("1200"), canon_number("1200.0"))
        self.assertEqual(canon_number("1.2E3"), "1200")
        self.assertEqual(canon_number("0.14"), "0.14")
        self.assertIsNone(canon_number("abc"))

    def test_backup_round_trip_and_password(self):
        self.vault.token_for("PERSON", "k", "Иванов")
        blob = self.vault.export_backup("пароль-123")
        self.assertNotIn("Иванов".encode("utf-8"), blob)
        other = Vault(Path(self.tmp.name) / "other.bin", key=os.urandom(32))
        other.import_backup(blob, "пароль-123")
        self.assertEqual(other.lookup(KIND_BY_CODE["PERSON"], 1).original, "Иванов")
        with self.assertRaises(crypto.KeyErrorSafe):
            other.import_backup(blob, "неверный")

    def test_import_refuses_to_mix_conflicting_tokens(self):
        self.vault.token_for("PERSON", "a", "Иванов")
        blob = self.vault.export_backup("secret1")
        other = Vault(Path(self.tmp.name) / "other.bin", key=os.urandom(32))
        other.token_for("PERSON", "b", "Петров")        # тот же Name1, другой человек
        with self.assertRaises(ValueError):
            other.import_backup(blob, "secret1")
        self.assertEqual(other.lookup(KIND_BY_CODE["PERSON"], 1).original, "Петров")


if __name__ == "__main__":
    unittest.main()
