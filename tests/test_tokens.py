import unittest

from anonymizer.tokens import EMAIL_TOKEN_RE, KIND_BY_CODE, TOKEN_RE, base_of, format_token, parse_match


def found(text: str) -> list[str]:
    return [m.group(0) for m in TOKEN_RE.finditer(text)] + [m.group(0) for m in EMAIL_TOKEN_RE.finditer(text)]


class TokenGrammarTests(unittest.TestCase):
    def test_tokens_are_short_and_readable(self):
        self.assertEqual(format_token(KIND_BY_CODE["PERSON"], 1), "Name1")
        self.assertEqual(format_token(KIND_BY_CODE["ORG"], 12, 3), "Company12_3")
        self.assertEqual(format_token(KIND_BY_CODE["EMAIL"], 4), "email4@example.com")
        self.assertEqual(format_token(KIND_BY_CODE["EMAIL"], 4, 2), "email4_2@example.com")

    def test_plain_and_variant_tokens_are_found(self):
        self.assertEqual(found("Name1 и Company12_3, City7."), ["Name1", "Company12_3", "City7"])

    def test_number_is_data_not_text(self):
        """Name1 и Name10 — разные люди: короткий токен не должен находиться внутри длинного."""
        self.assertEqual(found("Name10"), ["Name10"])
        m = TOKEN_RE.search("Name10")
        self.assertEqual(parse_match(m)[1], 10)

    def test_damage_done_by_language_models_is_tolerated(self):
        for text in ("**Name1**", "`Name1`", "name1", "NAME1", "(Name1)", "«Name1»", "Name1.", "Name1,", "Name1\\_2"):
            with self.subTest(text=text):
                self.assertTrue(found(text), text)
        self.assertEqual(parse_match(TOKEN_RE.search("Name1\\_2"))[2], 2)

    def test_cyrillic_lookalikes_do_not_hide_a_token(self):
        # «Nаme1»: русская «а»; «Сompany3»: русская «С»
        self.assertEqual(len(found("Nаme1")), 1)
        self.assertEqual(len(found("Сompany3")), 1)
        self.assertEqual(parse_match(TOKEN_RE.search("Nаme1"))[0].code, "PERSON")

    def test_email_tokens_keep_the_shape_of_an_address(self):
        self.assertEqual(found("пишите на email5@example.com."), ["email5@example.com"])
        self.assertEqual(found("Email5@Example.com"), ["Email5@Example.com"])

    def test_ordinary_text_is_not_a_token(self):
        for text in ("Names1", "rename1", "Company", "Name", "Name-1", "my_Name1x", "Name1x", "x1Name1", "Company1_abc"):
            with self.subTest(text=text):
                self.assertEqual([t for t in found(text) if t.lower() != "company1"], [], text)

    def test_base_of_strips_the_variant(self):
        self.assertEqual(base_of("Name7_2"), "Name7")
        self.assertEqual(base_of("email7_2@example.com"), "email7@example.com")
        self.assertIsNone(base_of("Hello"))

    def test_russian_ending_glued_to_a_token_does_not_break_it(self):
        self.assertEqual(found("Name1у ответил"), ["Name1"])


if __name__ == "__main__":
    unittest.main()
