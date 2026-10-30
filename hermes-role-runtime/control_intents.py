"""Conservative primary-text clauses; never interprets quotes or negation."""
import re

UNSAFE = re.compile(r'[?？`"“”>]|不要|不得|禁止|如果|假如|引用|转述|他说|她说|不需要|暂不')
SELECTION_VERB = r'(?:请)?(?:现在)?(?:先)?(?:切换(?:到|至)?(?:需求)?|转(?:到|至)?|选择(?:需求)?|激活(?:需求)?|启用(?:需求)?|开始(?:做|开发)?(?:需求)?|开发(?:需求)?|推进(?:需求)?|执行(?:需求)?|做(?:需求)?)'
ADVANCE = r'(?:并)?(?:自动推进|进行开发|开始开发|后台推进|全程推进|自动完成)'


def clauses(text, *, allow_unsafe=False):
    if not isinstance(text, str) or (not allow_unsafe and UNSAFE.search(text)):
        raise ValueError('AMBIGUOUS_CONTROL_SOURCE')
    values = [p.strip() for p in re.split(r'[，,。；;\n]|然后|随后|并且', text) if p.strip()]
    if not values or len(values) > 16:
        raise ValueError('BOUNDED_EXPLICIT_CONTROL_ACTIONS_REQUIRED')
    return values


def selection_plan(action, selectors):
    try:
        parts = clauses(action)
    except ValueError:
        return None
    controls, found = [], None
    for part in parts:
        if re.fullmatch(ADVANCE, part) and found:
            continue
        paused = re.fullmatch(r'(?:请)?(?:先)?(?:停止|暂停)\s*(COORDINATOR|A|B|C|TESTER)', part)
        if paused and found is None:
            controls.append({'action': 'pause', 'role': paused[1]})
            continue
        matched = [value for value in selectors if isinstance(value, str) and re.fullmatch(
            SELECTION_VERB+r'\s*[:：]?\s*'+re.escape(value)+r'(?:\s*'+ADVANCE+r')?\s*[！!]?', part)]
        if len(matched) != 1 or found is not None:
            return None
        found = matched[0]
    return {'target': found, 'pre_controls': controls} if found else None
