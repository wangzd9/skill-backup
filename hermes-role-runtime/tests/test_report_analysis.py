"""Report transport/schema failures from 2026-10-31, using isolated examples."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from report_analysis import contract, decode, failure_details
from workflow_reports import (_analysis, _PERSONAL_SECTION_LIMITS,
    COORDINATOR_SECTIONS, PERSONAL_SECTIONS)


class ReportAnalysisTests(unittest.TestCase):
    def value(self, coordinator=False):
        sections = COORDINATOR_SECTIONS if coordinator else PERSONAL_SECTIONS
        value = {'evidence_ids':['event:1'], 'sections':{
            name:[{'label':'核验范围', 'text':'本窗口无新增，保持待核实。'}] for name in sections}}
        if coordinator:
            value.update(accomplishments='本窗口无新增。', issues='缺少当前结果。', next_steps='等待有效回执。')
        return value

    def validate(self, value, role='A'):
        return _analysis({'analysis':value, 'model':'isolated-model', 'model_request_id':'isolated-request',
                          'route':'unknown'}, {'role':role,'evidence_ids':['event:1']})

    def test_whole_json_fence_is_a_lossless_transport_wrapper(self):
        value = self.value()
        value['sections']['当前工作'][0]['text'] = '原文保留：路径 `a/b`、空格  两个与引号“数据”。'
        raw = json.dumps(value, ensure_ascii=False)
        for output in (raw, '\n```json\n'+raw+'\n```\n', '```JSON\r\n'+raw+'\r\n```', '```\n'+raw+'\n```'):
            parsed, transport = decode(output)
            self.assertEqual(value, parsed)
            self.assertIn(transport, ('json','json_fence'))

    def test_damaged_json_is_never_guessed_or_repaired(self):
        raw = json.dumps(self.value(), ensure_ascii=False)
        for output in (raw[:-1]+')}', raw[:-2]+']]}}', raw[:-1], '```json\n'+raw,
                       '解释\n```json\n'+raw+'\n```', raw+'\n{}',
                       '```json\n'+raw+'\n```\n```json\n{}\n```', '[SILENT]'):
            with self.subTest(output=output[-50:]), self.assertRaises(ValueError):
                decode(output)

    def test_ambiguous_keys_nonstandard_numbers_and_nonobjects_are_rejected(self):
        for raw, code in (('{"sections":{},"sections":{}}','REPORT_JSON_DUPLICATE_KEY'),
                          ('{"sections":{"当前工作":[],"当前工作":[]}}','REPORT_JSON_DUPLICATE_KEY'),
                          ('{"value":NaN}','REPORT_JSON_NONFINITE_NUMBER'),
                          ('[]','REPORT_JSON_OBJECT_REQUIRED')):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, code):
                decode(raw)

    def test_model_supplies_typed_facts_and_script_adds_bold_labels(self):
        value = self.value()
        before = copy.deepcopy(value)
        analysis = self.validate(value)
        self.assertEqual(before, value)
        self.assertEqual(['**核验范围：** 本窗口无新增，保持待核实。'],analysis['sections']['当前工作'])
        coordinator = self.validate(self.value(True), 'COORDINATOR')
        self.assertEqual('**核验范围：** 本窗口无新增，保持待核实。',coordinator['sections']['整体目标与进度'])

    def test_typed_bullets_keep_semantic_validation(self):
        cases = [({'label':'状态','text':'待核实。','extra':'隐藏字段'}, 'REPORT_STRUCTURED_BULLET_INVALID'),
                 ({'label':'**状态**','text':'待核实。'}, 'REPORT_STRUCTURED_BULLET_INVALID'),
                 ({'label':'状态','text':'第一行\n第二行'}, 'REPORT_STRUCTURED_BULLET_INVALID'),
                 ({'label':'状态','text':'这是长段'*80}, 'BOLD_CHINESE_REPORT_BULLET_REQUIRED'),
                 ({'label':'状态','text':'第一项成果。第二项成果。'}, 'PERSONAL_REPORT_ONE_FACT_PER_BULLET_REQUIRED')]
        for bullet, code in cases:
            value = self.value();value['sections']['当前工作']=[bullet]
            with self.subTest(code=code), self.assertRaisesRegex(ValueError,code):
                self.validate(value)

    def test_no_dropping_overflow_or_forged_references(self):
        value=self.value();value['sections']['本次完成'] *= 4
        with self.assertRaisesRegex(ValueError,'PERSONAL_REPORT_BULLETS_REQUIRED:本次完成'):
            self.validate(value)
        value=self.value();value['evidence_ids']=['event:999']
        with self.assertRaisesRegex(ValueError,'REPORT_EVIDENCE_REFERENCE_MISMATCH'):
            self.validate(value)

    def test_shared_contract_uses_actual_limits_and_valid_example(self):
        for role in ('A','COORDINATOR'):
            prompt=contract(role)
            value=json.loads(prompt.split('：\n',1)[1])
            self.validate(value,role)
            self.assertIn('final仅输出一个完整JSON对象',prompt)
            self.assertIn('程序负责',prompt)
        self.assertEqual(4,_PERSONAL_SECTION_LIMITS['当前工作'])
        self.assertIn('当前工作=4',contract('A'))
        self.assertIn('本次完成=3',contract('A'))

    def test_parse_diagnostics_record_position_without_raw_model_text(self):
        try:
            decode('{"sections":BAD_MODEL_TEXT}')
        except json.JSONDecodeError as exc:
            result=failure_details(exc,'json_parse')
        self.assertEqual('REPORT_MODEL_OUTPUT_INVALID',result['reason_code'])
        self.assertEqual('json_parse',result['failure_stage'])
        self.assertEqual({'line':1,'column':13,'position':12},result['parse_error'])
        self.assertNotIn('BAD_MODEL_TEXT',json.dumps(result))


if __name__ == '__main__':
    unittest.main()
