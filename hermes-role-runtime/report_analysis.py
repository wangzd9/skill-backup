"""One analysis-only model contract and lossless JSON transport decoding.

Rendering and business evidence checks stay in workflow_reports. This module
never calls a model, repairs damaged JSON, or changes report/task state.
"""
import json
import re


def contract(role):
    from workflow_report_text import presentation_instruction
    from workflow_reports import (ANALYSIS_FIELDS, COORDINATOR_SECTIONS,
        PERSONAL_SECTIONS, _COORDINATOR_SECTION_LIMITS, _PERSONAL_SECTION_LIMITS)
    coordinator = role == 'COORDINATOR'
    sections = COORDINATOR_SECTIONS if coordinator else PERSONAL_SECTIONS
    limits = _COORDINATOR_SECTION_LIMITS if coordinator else _PERSONAL_SECTION_LIMITS
    example = {'evidence_ids': [], 'sections': {
        name: [{'label': '核验范围', 'text': '未取得本区事实，待核实。'}] for name in sections}}
    if coordinator:
        example = {**{name: '未取得相关事实，待核实。' for name in ANALYSIS_FIELDS}, **example}
    return ('本回合是只读时段总结的分析阶段，final仅输出一个完整JSON对象。'
        '程序负责标题、分区、bullet、粗体、时间、角色状态和用量排版；不要输出Markdown报告、代码围栏、解释前后缀或静默控制标记。'
        '即使无新增也必须返回完整JSON和全部分区，不适用业务poll的静默输出规则。'
        '顶层键必须且只能是 ' + '、'.join(example) + '；'
        'evidence_ids只引用输入facts.evidence_ids内的原样ID，没有引用时用空数组。'
        'sections键与示例完全一致且保持该顺序，每区是非空数组；上限分别为' +
        '、'.join(name+'='+str(limits[name]) for name in sections) + '条。'
        '每条只含label和text两个字符串字段：label为简短中文字段名，不带冒号、换行或Markdown；'
        'text用简体中文写一件事，不加列表前缀或换行；连同字段名和排版每条不超过240字。'
        '同一事实的条件可用分号连接，不把多项独立事实拼成长段；缺事实写待核实，无新增如实写无新增。'
        + presentation_instruction() +
        '核验时间、窗口、角色状态、证据引用、用量与观察来源由程序补入，不在sections重复。'
        '示例仅说明结构，不是本次事实，不可把示例结论当核验结果：\n' +
        json.dumps(example, ensure_ascii=False, separators=(',', ':')))


def decode(output):
    """Allow only a whole JSON document or one whole closed JSON code fence."""
    if not isinstance(output, str):
        raise ValueError('REPORT_MODEL_OUTPUT_TEXT_REQUIRED')
    body = output.strip()
    transport = 'json'
    fenced = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```', body,
                          flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        body = fenced.group(1)
        transport = 'json_fence'

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('REPORT_JSON_DUPLICATE_KEY')
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError('REPORT_JSON_NONFINITE_NUMBER')

    value = json.loads(body, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('REPORT_JSON_OBJECT_REQUIRED')
    return value, transport


def normalize(analysis, role):
    """Render typed labels without altering facts; preserve legacy validation."""
    from workflow_report_text import plain_text
    if not isinstance(analysis.get('sections'), dict):
        return analysis
    sections = dict(analysis['sections'])
    for heading, items in sections.items():
        if isinstance(items, str):
            sections[heading] = plain_text(items)
            continue
        if not isinstance(items, list):
            continue
        rendered = []
        typed = False
        for item in items:
            if isinstance(item, dict):
                typed = True
                label, text = item.get('label'), item.get('text')
                if (set(item) != {'label', 'text'} or not isinstance(label, str)
                        or not 1 <= len(label) <= 30 or label != label.strip()
                        or not re.search(r'[\u3400-\u9fff]', label)
                        or re.search(r'[*`\r\n：:]', label)
                        or not isinstance(text, str) or not text.strip()
                        or re.search(r'[\r\n]', text)):
                    raise ValueError('REPORT_STRUCTURED_BULLET_INVALID:' + heading)
                rendered.append('**' + plain_text(label) + '：** ' + plain_text(text))
            else:
                rendered.append(plain_text(item) if isinstance(item, str) else item)
        if typed:
            if role == 'COORDINATOR':
                if any(not isinstance(item, str) for item in rendered):
                    raise ValueError('REPORT_STRUCTURED_BULLET_INVALID:' + heading)
                sections[heading] = '\n'.join(rendered)
            else:
                sections[heading] = rendered
        else:
            sections[heading] = rendered
    return {**analysis, 'sections': sections}


def failure_details(exc, stage):
    """Keep precise safe diagnostics, without copying provider output/errors."""
    code, _, section = str(exc).partition(':')
    if not re.fullmatch(r'[A-Z_]+', code):
        code = {'json_parse':'REPORT_MODEL_OUTPUT_INVALID',
                'model_request':'REPORT_MODEL_REQUEST_FAILED',
                'input_collection':'REPORT_INPUT_COLLECTION_FAILED'}.get(stage,'REPORT_VALIDATION_FAILED')
    result = {'error': type(exc).__name__, 'reason_code': code, 'failure_stage': stage}
    if isinstance(exc, json.JSONDecodeError):
        result['parse_error'] = {'line': exc.lineno, 'column': exc.colno, 'position': exc.pos}
    from workflow_reports import COORDINATOR_SECTIONS, PERSONAL_SECTIONS
    if section in (*COORDINATOR_SECTIONS, *PERSONAL_SECTIONS):
        result['reason_section'] = section
    return result
