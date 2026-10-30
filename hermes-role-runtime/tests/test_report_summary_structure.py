"""Delivery summaries must reach the group with structure, not as one prose block."""
import unittest

from workflow_reports import _structure_summary


REAL_SUMMARY = '''MN-03 attempt4 返修交付：OCR b02-001（medium/bug，必修）+ 9 条 low 同批处置。head=ddbb12d78edc7e9cdca49bffb47345744e6f09fc，分支 feat/a100-multi-node/mn-03-4。

一、必修 b02-001：node_path_service.py resolve_frame_node 静默返回 'B'，与 AC MN-AC-07 矛盾。

二、9 条 low 逐条处置：
1) b01-001 approved_roots 键加 _require_str，混入 int 键不再抛 TypeError。
2) b01-002 sign_node_plan 改 copy.deepcopy，切断别名。

三、新增测试：75 → 88 passed。'''


class SummaryStructuringTest(unittest.TestCase):
    def test_real_summary_gains_bold_and_list_structure(self):
        out = _structure_summary(REAL_SUMMARY)
        self.assertGreaterEqual(out.count('**') // 2, 5)
        self.assertIn('\n\n', out)
        self.assertIn('- ', out)

    def test_finding_ids_and_hashes_become_code_spans(self):
        out = _structure_summary(REAL_SUMMARY)
        self.assertIn('`b02-001`', out)
        self.assertIn('`b01-001`', out)
        self.assertIn('`ddbb12d78edc7e9cdca49bffb47345744e6f09fc`', out)

    def test_section_and_item_markers_are_bold(self):
        out = _structure_summary(REAL_SUMMARY)
        self.assertIn('**一、', out)
        self.assertIn('**二、', out)
        self.assertIn('- **1）**', out)

    def test_no_adjacent_bold_spans(self):
        out = _structure_summary(REAL_SUMMARY)
        self.assertNotIn('** **', out)

    def test_already_formatted_text_is_not_rewrapped(self):
        text = '**结论：** 已通过。\n\n- **一、** 细节。'
        self.assertEqual(_structure_summary(text), text)

    def test_prose_without_structure_survives_unchanged_in_wording(self):
        text = '这是一段没有任何编号也没有字段冒号的说明文字，只应原样保留。'
        out = _structure_summary(text)
        self.assertEqual(out, text)

    def test_blank_lines_are_collapsed_not_multiplied(self):
        out = _structure_summary('第一段。\n\n\n\n第二段。')
        self.assertNotIn('\n\n\n', out)
        self.assertIn('\n\n', out)

    def test_invalid_input_does_not_raise(self):
        for value in (None, '', '   ', 42, [], {}):
            self.assertEqual(_structure_summary(value), '' if not isinstance(value, str) else value)

    def test_tail_punctuation_glues_to_bold_without_space(self):
        out = _structure_summary('一、必修 b02-001：细节内容。')
        self.assertNotIn('** ：', out)


if __name__ == '__main__':
    unittest.main()
