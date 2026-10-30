#!/usr/bin/env python3
"""Migrate existing role cron payloads without changing schedules, destinations or lifecycle."""
import argparse
import json
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_store import atomic_text
from runtime_common import save_json


SUMMARY_PROMPT = ('这是独立只读时段总结。仅根据前置脚本提供的当前事实、时间和本人用量，'
                  '面向用户默认使用简体中文，直接给结论、依据、阻塞和下一步；不输出英文工作草稿或逐步自问自答。'
                  '进行分析整理并输出最终群报；不领取/续租/释放业务租约，不确认业务pending，'
                  '不运行开发、测试、OCR或强审，不复用历史聊天印象。'
                  '证据冲突或采集失败要明确，模型未生成总结时不能冒充成功。'
                  '按输入约定的协调者七区/个人四区及本地云端缓存用量结构汇报。'
                  '本回合无工具，脚本已按引用展开当前证据；缺口明确标注。'
                  '最终正文由原cron通道投递，不另发消息。')


def job_updates(binding, jobs):
    updates = {}
    for role, cfg in binding['roles'].items():
        identifier = cfg.get('report_job_id')
        if identifier:
            if identifier not in jobs: raise ValueError('bound report job missing: '+identifier)
            updates[identifier] = ({'no_agent': True, 'skills': [], 'context_from': [],
                'prompt': '只投递脚本输出的执行者A/B/C与测试者状态短报。'} if role == 'COORDINATOR' else
                {'no_agent': False, 'skills': [], 'context_from': [], 'enabled_toolsets': ['no_mcp'],
                 'prompt': SUMMARY_PROMPT})
        morning = cfg.get('morning_report_job_id')
        if morning:
            if morning not in jobs: raise ValueError('bound morning job missing: '+morning)
            updates[morning] = {'no_agent': False, 'skills': [], 'context_from': [],
                                'enabled_toolsets': ['no_mcp'], 'prompt': SUMMARY_PROMPT}
        poll = cfg.get('poll_job_id')
        if poll:
            if poll not in jobs: raise ValueError('bound poll job missing: '+poll)
            old = jobs[poll].get('prompt', '')
            marker = '\n\n[role-workflow-v3]'
            old = old.split(marker)[0]
            addition = (marker+'\n先运行绑定runtime_tools.py context-input --project '+binding['project_root']+' --role '+role+
                '，只读最新检查点与本轮相关材料；同会话参考版本未变不全文重读，新会话/压缩后重读。'
                '领取使用task-claim固定入口，成功ACK后脚本发一句领取通知，final不复发领取通知。'
                '新业务租约绑定已核实的长期worker PID/start，不能使用临时shell PID；'
                '旧未绑定owner在自己的安全检查点核实后renew绑定，不由其他角色代接管。'
                '交付/失败/阻塞由本人模型总结；角色名后追加可接单状态。'
                '群内答复、cron final、交付/失败/阻塞及定时汇报默认用简体中文；技术标识、命令、路径及必要错误原文可保留。'
                'final只写已核实的结论、证据、阻塞和下一步；不把英文工作草稿、逐步自问自答或待检查设想作为最终汇报。'
                '达到64KiB或400行的会话由role-record自动归档；首次旧大文档先建立已核实检查点，'
                '按document-archive归档本角色session。协调者在安全检查点语义整理TODO/HANDOFF，'
                '保留全部未完成任务、当前门禁和恢复动作，禁止机械tail。'
                '控制动作或开发/验证阶段完成后在安全检查点结束回合；剩余可执行动作通过唯一continuation保存，'
                '处理后以输入指纹和真实处理证据continuation-ack，纯等待不建续跑。'
                '所有业务写入后重新poll再ACK已处理指纹，不吞未读输入。'
                '20分钟定时快报仅脚本状态；11/15/18及08点时段报告由独立只读模型处理，不占业务槽。'
                '强审run仅短提交独立systemd服务，查询实际service/结果后裁决；不在cron terminal里长跑。')
            updates[poll] = {'prompt': old+addition}
    return updates


def configure(project, home, *, apply=False):
    project, home = Path(project).resolve(), Path(home).resolve()
    sys.path.insert(0, str(home/'hermes-agent'))
    from cron.jobs import get_job, update_job
    binding = json.loads((project/'ROLE-BINDINGS.json').read_text())
    ids = {v for cfg in binding['roles'].values() for k,v in cfg.items() if k.endswith('job_id') and v}
    before = {identifier:get_job(identifier) for identifier in ids}
    changes = job_updates(binding, before)
    result = {'project':str(project), 'applied':apply, 'jobs':{i:{k:v for k,v in u.items() if k != 'prompt'} for i,u in changes.items()}}
    if not apply: return result
    runtime = Path(__file__).resolve().parents[1]
    interpreter = home/'hermes-agent/venv/bin/python'
    if not interpreter.is_file():
        raise ValueError('Hermes interpreter missing: '+str(interpreter))
    for role, cfg in binding['roles'].items():
        for key, kind in (('report_job_id','phase'), ('morning_report_job_id','morning')):
            identifier = cfg.get(key)
            if not identifier or role == 'COORDINATOR' and kind == 'phase': continue
            path = Path(before[identifier]['script'])
            cmd = [str(interpreter),str(runtime/'report_summary.py'),kind,
                   '--project',str(project),'--role',role,'--hermes-home',str(home)]
            atomic_text(path, '#!/bin/sh\nexec '+' '.join(shlex.quote(v) for v in cmd)+'\n')
        for key in ('poll_job_id','report_job_id','morning_report_job_id'):
            identifier=cfg.get(key)
            if identifier in changes: update_job(identifier,changes[identifier])
    protected = ('id','enabled','state','schedule','origin','deliver','next_run_at')
    for identifier, old in before.items():
        new = get_job(identifier)
        if any(old.get(k) != new.get(k) for k in protected):
            raise RuntimeError('cron lifecycle/destination unexpectedly changed: '+identifier)
    policy = json.loads((home/'role-reporting-policy.json').read_text())
    policy.pop('no_agent',None)
    policy.update(policy_version='role-workflow-v3', coordinator_status_no_agent=True,
                  summary_requires_model=True, role_job_ids=sorted(changes))
    save_json(home/'role-reporting-policy.json',policy)
    result['protected_fields_preserved'] = True
    return result


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',required=True); p.add_argument('--home',default=str(Path.home()/'.hermes'))
    p.add_argument('--apply',action='store_true')
    a=p.parse_args(); print(json.dumps(configure(a.project,a.home,apply=a.apply),ensure_ascii=False,indent=2))
