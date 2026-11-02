"""Bounded, complete prose units with explicit access to the original evidence."""
import re


def presentation_instruction():
    return ('对外汇报用普通人能看懂的简体中文，保留既定七区/四区、角色状态和重点加粗。'
        '每条先说具体功能、已经做成什么、还差什么、谁处理及下一步，不用任务编号或状态码代替说明。'
        '任务编号仅在确需区分任务时放括号；事件编号、完整版本号、工作单ID、分支路径和原始错误留在证据附件，用户追问再展开。'
        'MySQL、NAS等必要产品名可保留；OCR首次解释为代码审查，NAS可说测试文件存储。'
        '把PENDING/REVIEW/RESULT_READY/NOT_RUN等写成待开始/等待审查/审查结果已出、待协调者处理/尚未测试。'
        '不用run、work、park、writer、findings、caller、supplemental_verification、核退、落账、门禁等内部说法；'
        '例如“下游work已park，control=DRAINING”应说“先补做前置功能，原任务和进度已保留，完成后自动继续”。'
        '已开发、开发者自测、独立测试、已批准、已合入、已上线必须分别说清；未执行、未通过、结果未知不得改成通过。'
        '需要用户决定的只列正式待决事项，说明问题、影响和选项；协调者职责内的工作写“由我处理”，不凭空追加拍板项。'
        '不要把历史检查点、内部核查过程或额外时间线粘进当前报告；需要历史时用中文概括并标明时间。')


def prose_parts(value):
    """Keep commands and file/URL references byte-for-byte in presentation."""
    protected = (r'```[\s\S]*?```|`(?:python[0-9.]*|pytest|git|docker|bash|zsh|sh|env|curl|rg|ruff|alembic|uv|node|npm)\b[^`\n]*`|'
                 r'`[^`\n]*(?:/|\\|\.(?:py|md|json|yaml|yml|xml|txt|sh|sql))[^`\n]*`|'
                 r'https?://[^\s`，；。]+|(?<![\w-])(?:/|\.{1,2}/)[\w./{}:\-]+|'
                 r'(?<![\w-])[\w.-]+(?:/[\w.{}-]+)*\.(?:py|md|json|yaml|yml|xml|txt|sh|sql)\b')
    return re.split('(' + protected + ')', str(value or ''))


def plain_text(value):
    """Translate known reporting vocabulary; never change evidence or IDs."""
    parts = prose_parts(value)
    phrases = {'原生写执行全部核退后切换': '正在运行的任务全部结束后切换',
        '全部原生writer核退': '正在运行的任务全部结束',
        '原生依赖调度': '后台自动安排', '原生调度': '后台',
        '原生安全点': '正在运行的任务结束', '原生writer': '正在运行的任务',
        '本次账本快照': '本次查询', '适用最终验收': '要求的整体验收',
        '固定候选': '待审版本', '当前候选': '当前待审版本',
        '独立验证': '独立测试', '冻结合同': '已确认的任务要求',
        '验收门禁': '验收要求', '串行围栏': '排队限制', '串行门': '审查排队',
        '门禁': '必要检查', '核退': '确认结束', '落账': '记录',
        '已记录的接续计划': '上次记录的下一步', '历史检查点': '上次记录',
        '未关闭门禁': '还缺哪些检查', '最近最终验证回执': '最近验收结果',
        '接续条件': '继续条件', '接续': '继续处理', '工作单': '任务记录',
        '正式问答': '需要用户决定的问题', '固定版本': '指定版本',
        '补验': '补充测试', '回派': '安排返修', '返修谱系': '返修记录'}
    words = {'OCR_SERIAL_ACTION_STILL_ACTIVE_OR_UNKNOWN': '代码审查正在运行，或结果尚未确认',
        'NO_VERIFICATION_READY': '暂无可开始的测试', 'NOT_VERIFIED': '尚未验证',
        'NOT_RUN': '尚未执行', 'RESULT_READY': '结果已出，待协调者处理',
        'INCOMPLETE': '结果不完整', 'SUBMITTED': '已提交，等待结果',
        'UNKNOWN': '结果尚未确认', 'UNVERIFIED': '尚未核实', 'UNAVAILABLE': '暂不可用',
        'PASS': '通过', 'FAIL': '未通过', 'FAILED': '执行失败', 'BLOCKED': '受阻',
        'PENDING': '待开始', 'RUNNING': '进行中', 'REVIEW': '等待审查',
        'COMPLETED': '已完成', 'DRAINING': '等待当前执行结束', 'OPEN': '可用',
        'REWORK': '需要返修', 'CANCELLED': '已取消', 'COMMITTED': '已登记',
        'QUEUED': '已排队', 'DEVELOPING': '开发中', 'TESTING': '测试中',
        'independent_test': '独立测试', 'supplemental_verification': '补充测试',
        'FINAL-INTEGRATION': '整体验收', 'write_scope': '允许修改的范围',
        'findings': '审查发现的问题', 'worker': '后台处理程序', 'caller': '调用位置',
        'writer': '正在执行的任务', 'owner': '负责人', 'run': '需求', 'work': '任务记录', 'park': '保留',
        'head': '提交版本', 'attempt': '轮次', 'passed': '项通过', 'skip': '项跳过',
        'Backend': '后端服务', 'upgrade': '升级', 'downgrade': '回滚', 'claim': '领取',
        'scope': '任务范围', 'AC': '验收标准'}
    # Paths, protocol names and task IDs keep their exact spelling. Only bare
    # words in prose are translated, not substrings inside technical identities.
    pattern = r'(?<![\w.\-])(' + '|'.join(map(re.escape, sorted(words, key=len, reverse=True))) + r')(?![\w.\-])'
    for index in range(0, len(parts), 2):
        text = parts[index]
        for source in sorted(phrases, key=len, reverse=True):
            text = text.replace(source, phrases[source])
        parts[index] = re.sub(pattern, lambda match: words[match.group(0)], text)
    return ''.join(parts)


def language_error(text):
    """Catch internal status dumps without banning product names or paths."""
    text = ''.join(prose_parts(text)[::2])
    if re.search(r'\b(?:requires_user_approval|current_attention|task_phase|write_scope|'
                 r'waiting_external|decisions|merges)\s*[=:]', text):
        return 'INTERNAL_STATE_IN_USER_REPORT'
    if re.search(r'(?<![\w.\-])(?:RESULT_READY|NOT_RUN|NOT_VERIFIED|INCOMPLETE|SUBMITTED|'
                 r'UNKNOWN|UNVERIFIED|PENDING|REVIEW|DRAINING|COMPLETED|NO_VERIFICATION_READY|'
                 r'independent_test|supplemental_verification|OCR_SERIAL_ACTION_STILL_ACTIVE_OR_UNKNOWN)'
                 r'(?![\w.\-])', text):
        return 'PLAIN_CHINESE_REPORT_REQUIRED'
    return None


def delivery_summary(value, limit=500):
    for paragraph in re.split(r'\n\s*\n', str(value or '').strip()):
        plain = paragraph.replace('**', '').replace('`', '').strip()
        if not re.fullmatch(r'(?:[一二三四五六七八九十0-9]+[、.．：:]?\s*)?'
                            r'(?:交付结论|交付内容|当前工作|本次完成|交付说明)[：:]?', plain):
            # Delivery prose is historical. The current stage/checks are
            # rendered separately, so do not replay its old "waiting" suffix.
            paragraph = paragraph.replace('**', '').strip()
            paragraph = re.sub(r'^TASK-[A-Z0-9-]+\s*(?:attempt\s*\d+)?\s*(?:[（(][^）)]+[）)])?\s*交付[：:]\s*',
                               '', paragraph)
            paragraph = re.sub(r'[，；。]\s*(?:等待|待)(?:独立测试|独立验证|OCR|审查)[^。]*[。]?$','',paragraph)
            return excerpt(paragraph, limit)
    return ''


def excerpt(value, limit=205):
    text = plain_text(value).strip().replace('**', '').replace('`', '')
    lines = [line.strip().lstrip('- ').strip() for line in text.splitlines() if line.strip()]
    substantive = [line for line in lines if not re.fullmatch(
        r'(?:#{1,6}\s*)?(?:[一二三四五六七八九十0-9]+[、.．：:]?\s*)?'
        r'(?:交付结论|当前工作|本次完成|阻塞与依赖|下一步计划|验证结果|交付说明)[：:]?', line)]
    text = re.sub(r'\s+', ' ', '；'.join(substantive or lines)).strip()
    if len(text) <= limit:
        return text
    suffix = '；完整记录见同次进度附件。'
    budget = limit - len(suffix)
    units = re.findall(r'.*?[。；;！？](?:\s*|$)|.+$', text)
    chosen = ''
    for unit in units:
        unit = unit.strip()
        if len(chosen + unit) > budget:
            break
        chosen += unit
    if not chosen:
        return '原始记录较长，完整内容见同次进度附件；当前条件以本报告门禁及接续项为准。'
    return chosen.rstrip('；;。') + suffix
