#!/usr/bin/env python3
"""Build a fixed catalog-anchored role pool without rebinding active owners."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import shutil
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime_common import read_json,save_json
from document_store import atomic_text

ROLES=('COORDINATOR','A','B','C','TESTER')
POLL_PROMPT=('这是固定角色池的后台业务回合，角色为{role}。需求目录固定为{catalog}。'
    '实际业务范围以本轮冻结的HERMES_ROLE_PROJECT_ROOT、HERMES_ROLE_REQUIREMENT_ID和前置脚本为准；'
    '不得使用上一需求的任务、base、attempt、令牌或授权。后台沿用已接受需求范围内的执行授权，'
    '无需群聊用户每轮授予全部权限。普通角色群问答默认只读，与本后台权限分别判断。'
    '先读取当前角色Skill；用runtime_tools.py context-input读取本轮最新检查点、有效派单、pending和参考版本。'
    '用role_runtime.py worker-identity核实自己的长期cron worker身份，领取本角色当前项目租约；'
    'owner忙或输入/代际不匹配则停止业务动作，不抢占、不借用其他worker。'
    '协调者只负责当前需求规划接续、派单、证据核验、OCR/强审和集成裁决，不代替执行者开发或测试者验收。'
    '执行者只做有效派单开发、自测、返修；测试者只做固定快照独立测试与取证。'
    '缺少可执行计划时由协调者按本需求PRD/TODO建立计划与有效派单，执行者/测试者不能自行发明任务。'
    '领取统一task-claim；完成/失败/阻塞由模型核实回执后用中文总结。'
    '保留原验收、测试、授权、生产门禁；独立OCR/强审沿原服务身份、授权和额度运行，cron只提交查询。'
    '业务写入完成后重新核输入，再精确ACK已处理指纹。开发/验证安全点保存唯一continuation后接续；'
    '待切换时先落检查点并释放业务槽，不杀旧工作、不清pending、不自动恢复暂停。'
    '只读最新活动文档，历史按索引取证；稳定参考同会话版本未变不重复读。'
    '所有面向用户输出使用简体中文，直接给结论、证据、阻塞和下一步；技术标识可保留原文。'
    '原cron投递最终正文，避免重复发消息；无有意义变化遵循既有静默约定。')


def build_pool(catalog,registry):
    catalog,registry=Path(catalog).resolve(),Path(registry).resolve()
    registered=read_json(registry);roles={};roots={};bindings=[]
    stable_fields=('skill','chat_id','poll_job_id','report_job_id','morning_report_job_id',
                   'idle_report_job_id','session_file','report_policy','skill_version')
    for entry in registered['projects']:
        root=Path(entry['project_root']).resolve();binding=read_json(root/'ROLE-BINDINGS.json');bindings.append(binding)
        for role,cfg in binding['roles'].items():
            if role not in ROLES:continue
            if role in roles:raise ValueError('角色重复绑定：'+role)
            roles[role]={key:cfg[key] for key in stable_fields if key in cfg};roots[role]=str(root)
            skill=Path.home()/'.codex/skills'/cfg['skill']/'SKILL.md'
            version=re.search(r'^  version: [\"\']?([0-9.]+)',skill.read_text(),re.MULTILINE) if skill.is_file() else None
            if version:roles[role]['skill_version']=version[1]
    if set(roles)!=set(ROLES):raise ValueError('当前固定五角色不完整，不能自动创建替代身份')
    requirements={}
    compatibility={'rosbag-catalog-scene-foundation','rosbag-catalog-scene-ui','rosbag-catalog-spray-pipeline'}
    table='\n'.join(line for line in catalog.read_text().splitlines() if line.startswith('| ['))
    for relative in re.findall(r'^\| \[[^\]]+\]\(([^)]+/README\.md)\)',table,re.MULTILINE):
        package=(catalog.parent/relative).resolve()
        if package.parent.parent!=catalog.parent or relative.startswith('archive/'):continue
        identifier=package.parent.name
        requirements[identifier]={'runtime_root':str(catalog.parent/'rosbag-catalog-integration' if identifier in compatibility else package.parent),
                                  'aliases':[str(package.parent)]}
    defaults={key:deepcopy(bindings[0][key]) for key in ('timezone','runtime_script','strong_review_runtime',
             'development_log','presentation_skill','presentation_version','observer_runtime','runtime_policy_version',
             'poll_policy','runtime_helpers','manual_interaction_policy','report_policy') if key in bindings[0]}
    defaults.update(control='ACTIVE',schema_version=1)
    defaults['report_policy']={k:v for k,v in defaults.get('report_policy',{}).items() if not k.endswith('_command')}
    defaults.setdefault('manual_interaction_policy',{})['busy_owner']='no_takeover_control_selection_only'
    defaults.setdefault('runtime_helpers',{})['plugin_version']='0.12.0'
    defaults['presentation_version']='1.12.0'
    current_id=next((key for key,cfg in requirements.items() if cfg['runtime_root']==roots['A']),None)
    if current_id is None:raise ValueError('现有执行者需求根未映射至总索引，必须明确兼容映射')
    return {'schema_version':1,'pool_id':'prd-five-roles','catalog_path':str(catalog),'registry_path':str(registry),
        'generation':0,'control':'ACTIVE','roles':roles,'requirements':requirements,
        'current':{'requirement_id':current_id,'runtime_root':roots['A'],'role_roots':roots,
                   'origin':'existing_bindings_preserved_no_new_business_authorization'},'binding_defaults':defaults}


def updates_for(pool,pool_path,runtime):
    updates={};wrappers={}
    for role,cfg in pool['roles'].items():
        for field,action in (('poll_job_id','poll'),('report_job_id','report' if role=='COORDINATOR' else 'phase'),
                             ('morning_report_job_id','morning'),('idle_report_job_id','idle-report')):
            identifier=cfg.get(field)
            if not identifier:continue
            updates[identifier]={'role_pool':{'path':str(pool_path),'role':role,'kind':'business' if action=='poll' else 'report'},
                'workdir':str(Path(pool['catalog_path']).parent)}
            if action=='poll':updates[identifier]['prompt']=POLL_PROMPT.format(role=role,catalog=pool['catalog_path'])
            wrappers[identifier]=(action,role)
    return updates,wrappers


def configure(catalog,registry,pool_path,home,*,apply=False):
    pool_path,home=Path(pool_path).resolve(),Path(home).resolve()
    pool=read_json(pool_path) if pool_path.exists() else build_pool(catalog,registry)
    updates,wrappers=updates_for(pool,pool_path,Path(__file__).resolve().parents[1])
    sys.path.insert(0,str(home/'hermes-agent'))
    from cron.jobs import get_job,update_job
    before={key:get_job(key) for key in updates}
    if any(not value for value in before.values()):raise ValueError('绑定job缺失')
    result={'pool':str(pool_path),'roles':list(pool['roles']),'requirements':len(pool['requirements']),
            'applied':apply,'jobs':sorted(updates),'existing_business_roots_preserved':True}
    if not apply:return result
    backup=home/'backups'/('role-pool-'+datetime.now().strftime('%Y%m%d-%H%M%S'));backup.mkdir(parents=True)
    shutil.copy2(home/'cron/jobs.json',backup/'jobs.json')
    shutil.copy2(registry,backup/'role-projects.json')
    if pool_path.exists():shutil.copy2(pool_path,backup/'role-pool.json')
    else:save_json(pool_path,pool)
    runtime=Path(__file__).resolve().parents[1];interpreter=home/'hermes-agent/venv/bin/python'
    for identifier,(action,role) in wrappers.items():
        path=Path(before[identifier]['script']);shutil.copy2(path,backup/path.name)
        mode=path.stat().st_mode & 0o777
        if action=='poll':
            command=[str(interpreter),str(Path.home()/'.codex/skills/complex-task-coordinator/scripts/role_observer.py'),
                     'pool-poll','--pool',str(pool_path),'--role',role]
        else:
            command=[str(interpreter),str(runtime/'pool_entry.py'),action,'--pool',str(pool_path),
                     '--role',role,'--hermes-home',str(home)]
        atomic_text(path,'#!/bin/sh\nexec '+' '.join(shlex.quote(word) for word in command)+'\n')
        path.chmod(mode)
        update_job(identifier,updates[identifier])
    protected=('id','enabled','state','schedule','origin','deliver','next_run_at')
    for identifier,old in before.items():
        fresh=get_job(identifier)
        if any(fresh.get(key)!=old.get(key) for key in protected):raise RuntimeError('job保护字段发生变化：'+identifier)
        if fresh.get('role_pool')!=updates[identifier]['role_pool']:raise RuntimeError('role_pool配置未持久化')
    result.update(backup=str(backup),protected_fields_preserved=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',default='/home/wang/work/doc/prd/README.md')
    parser.add_argument('--registry',default=str(Path.home()/'.hermes/role-projects.json'))
    parser.add_argument('--pool',default=str(Path.home()/'.hermes/role-pool.json'))
    parser.add_argument('--home',default=str(Path.home()/'.hermes'));parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();print(json.dumps(configure(args.catalog,args.registry,args.pool,args.home,apply=args.apply),ensure_ascii=False))
