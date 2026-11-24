"""翻译层测试：ANSI 剥离、行提取、词典命中、命令过滤。"""
import unittest

from buddy_bridge.translate import (
    LineCollector,
    Translator,
    glossary_lookup,
    is_translatable,
    strip_ansi,
)


class TestAnsi(unittest.TestCase):
    def test_strip_color(self):
        self.assertEqual(strip_ansi("\x1b[32mOK\x1b[0m"), "OK")

    def test_strip_cursor_and_osc(self):
        raw = "\x1b[2J\x1b[1;1H\x1b]0;title\x07text"
        self.assertEqual(strip_ansi(raw), "text")

    def test_empty(self):
        self.assertEqual(strip_ansi(""), "")


class TestLineCollector(unittest.TestCase):
    def test_split_lines(self):
        lc = LineCollector()
        out = []
        for chunk in ["hello", " world\r\nsecond line\r\n"]:
            out.extend(lc.feed(chunk))
        self.assertEqual(out, ["hello world", "second line"])

    def test_carriage_return_overwrite(self):
        lc = LineCollector()
        out = lc.feed("progress 10%\rprogress 90%\r\n")
        self.assertEqual(out, ["progress 90%"])

    def test_incomplete_line_buffered(self):
        lc = LineCollector()
        self.assertEqual(lc.feed("no newline yet"), [])
        self.assertEqual(lc.feed(" done\n"), ["no newline yet done"])


class TestTranslatable(unittest.TestCase):
    def test_accepts_sentence(self):
        self.assertTrue(is_translatable("Do you want to proceed with this change?"))
        self.assertTrue(is_translatable("The model returned an unexpected response"))

    def test_rejects_commands(self):
        for s in ["npm install -g @tencent-ai/codebuddy-code",
                  "$ bb codebuddy",
                  "git commit -m 'fix: bug here'",
                  "python -m unittest discover -s tests"]:
            self.assertFalse(is_translatable(s), s)

    def test_rejects_paths_and_urls(self):
        self.assertFalse(is_translatable("GET /api/v1/health 200"))
        self.assertFalse(is_translatable("open https://example.com/some/path now"))

    def test_rejects_chinese(self):
        self.assertFalse(is_translatable("这是一句已经翻译好的中文提示内容"))

    def test_rejects_symbols(self):
        self.assertFalse(is_translatable("✓"))
        self.assertFalse(is_translatable("----"))


class TestGlossary(unittest.TestCase):
    def test_exact_phrase(self):
        self.assertEqual(glossary_lookup("Do you want to proceed?"), "是否继续？")
        self.assertEqual(glossary_lookup("Permission denied"), "权限被拒绝")

    def test_template(self):
        got = glossary_lookup("Are you sure you want to delete this file?")
        self.assertIn("确定要", got)
        self.assertIn("删除", got)
        self.assertNotIn("  ", got)

    def test_unknown_returns_none(self):
        self.assertIsNone(glossary_lookup("Zzz qqq vvv wwww xxxx yyyy zzzz"))

    def test_no_chinese_space(self):
        got = glossary_lookup("Failed to connect to the server")
        self.assertEqual(got, "失败：连接到服务器")


class TestTranslator(unittest.TestCase):
    def test_glossary_path_no_network(self):
        t = Translator()  # 未配模型，不应发请求
        self.assertEqual(t.translate("Do you want to proceed?"), "是否继续？")
        self.assertEqual(t.stats()["glossary"], 1)

    def test_cache_hit(self):
        t = Translator()
        t.translate("Permission denied")
        t.translate("Permission denied")
        self.assertEqual(t.stats()["hits"], 1)

    def test_unknown_without_model(self):
        t = Translator()
        self.assertIsNone(t.translate("Some completely unknown English sentence here"))

    def test_batch(self):
        t = Translator()
        out = t.translate_many(["Permission denied", "Do you want to proceed?", ""])
        self.assertEqual(out[0], "权限被拒绝")
        self.assertEqual(out[1], "是否继续？")
        self.assertIsNone(out[2])


if __name__ == "__main__":
    unittest.main()
