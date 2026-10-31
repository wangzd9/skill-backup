"""Activate reviewed future-call seams without replacing in-flight state.

The native host already supports bounded source migrations. The same boundary
is used for the installed runtime's named functions; module globals, dispatch
slots, locks, running frames, process ownership and business rows stay intact.
"""
import ast
import hashlib
import os
from pathlib import Path
import sys
import threading

LOCK=threading.Lock()
TARGETS={
    '__init__.py':('RuntimePlugin.work_input_tool','RuntimePlugin.work_command_tool',
                   'RuntimePlugin.decision_context_tool','RuntimePlugin.on_api'),
    'workflow_sandbox.py':('tool_directive', '_run'),
    'requirement_runtime.py':('list_requirements',),
    'workflow_registration.py':('freeze_sources','packages','register_packages','prepared_package'),
    'business_ledger.py':('BusinessLedger._command','BusinessLedger._event','BusinessLedger.snapshot','BusinessLedger.admission_hint','BusinessLedger._release_idle_coordination',
        'BusinessLedger.publish_plan','BusinessLedger.dependency_readiness','BusinessLedger.stage_dependency_ids',
        '_stage_dependencies','_dependency_gate','BusinessLedger.merge_readiness',
        'BusinessLedger._registered_pending','BusinessLedger._select','BusinessLedger._identity','BusinessLedger.bind_execution',
        '_tester_candidate','BusinessLedger.deliver','BusinessLedger.release_work','BusinessLedger.end_execution','BusinessLedger.observe_execution_outcome','BusinessLedger._rework',
        'BusinessLedger.checkpoint','BusinessLedger.register_external_action','BusinessLedger.external_dispatch_readiness',
        'BusinessLedger.grant_external_authorization','BusinessLedger.grant_merge_authorization','BusinessLedger.control_request','BusinessLedger._authorization','BusinessLedger.decide',
        'BusinessLedger.record_service_artifact','BusinessLedger.reject_external_action',
        'BusinessLedger._evidence','BusinessLedger.work_inputs','BusinessLedger.ack_events','BusinessLedger.record_merge','BusinessLedger.publish_verification','BusinessLedger._acceptance'),
    'workflow_runtime.py':('prepare','prepare_report','recover_execution_outcomes','current_input',
        'collect_event_tick','request_business_wakeups','tool_decision','_tool_decision','_execute_command','_validated_command',
        '_control_clause','_authorization_limits','submit_control','project_events','verified_input_view'),
    'workflow_reports.py':('build_facts','_observation_facts','render_periodic','render_notification',
        '_analysis','_analysis_bullets','_coordinator_progress_format_error','_progress_report_format_error',
        '_live_role_label','_live_bullet','render_live_progress_fallback','_bounded_report_item',
        'failed_periodic_report','_validated_section_bullets','_periodic_render_format_error',
        '_read_progress_context',
        'transform_coordinator_notice'),
    'report_summary.py':('build_summary_input','complete_native_summary'),
    'workflow_notifications.py':('drain_business_notifications',),
    'workflow_views.py':('work_packet','fit_report'),
    'workflow_model_input.py':('_pointer','deduplicate','project'),
    'workflow_toolsets.py':('configure',),
    'workflow_coordination_log.py':('append',),
    'workflow_decision_context.py':('query_specs','query_contract','query_schema','validate_query','digest','sampling','_basis','_target','directory','startup_index','_bounded','_contract','_is_command','_objects','_source_checks','_source_status','prepare','_query','query'),
    'workflow_decision_metrics.py':('_root','_key','observe','request'),
    'workflow_delivery.py':('complete','evidence_directory','contract','preflight'),
    'workflow_business_progress.py':('progress','global_review_fences'),
    'workflow_ocr_selection.py':('freeze',),
    'workflow_sessions.py':('observe','tool_decision'),
    'workflow_capacity.py':('observe',),
    'workflow_verification_retry.py':('basis','claim','finish','blocked'),
    'workflow_work_memory.py':('validate','inspect'),
    'workflow_recovery.py':('guidance',),
    'workflow_verification_exceptions.py':('facts','_result_source','record','recover','_repair_context','resolve','reconcile','revalidation_ids','repair_descendant','repair_followup','closure_passes','blockers'),
    'prefix_audit.py':('PrefixAudit.record',),
    'workflow_service_recovery.py':('resume_readiness','resume','coverage_recovery','coverage_blocker'),
    'workflow_observation.py':('step','saved_health',),
    'observation_view.py':('health_view',),
    'activity_signals.py':('activity_step',),
    'workflow_activity.py':('_receipt_lock','_tool_database','_model_database_unlocked',
        '_database','_model_database','_fault','_record','_record_model',
        'read_activity','read_model_activity','observation_grace_for_tool'),
    'workflow_diagnostics.py':('query_closures','read_projection','observe','rebuild_projection',
        'publish_receipt','_project'),
    'report_contract.py':('_runtime_items','_role_next','_role_items'),
    'manual_access.py':('readonly_tool_decision',),
    'workflow_query.py':('history','checkpoint_history','review_action_projection','progress_context','_artifact','_paged_artifact',
        '_history_index','_task_summary','_role_summary','_bounded','live_progress','repo_state','ocr_gate_state'),
    'delivery_evidence.py':('preflight_test_request','_candidate_test_command','execute_candidate_tests','check_candidate_test_evidence','parse_test_results'),
    'workflow_dispatch.py':('_intent','request_role_wakeup','_persist'),
    'ocr_runtime.py':('prepare','preflight_receipt','preview','resolved_rules','validate_snapshot','worker'),
    'workflow_services.py':('_background_bytes','_freeze_background','NativeServices._cli','NativeServices.preflight',
        'NativeServices.recover_reference','NativeServices.submit','submit_service','collect_service_result',
        '_request_inputs','validate_native_action'),
    'workflow_contracts.py':('test_execution_fields','test_execution_rules','payload_schema','command_schema','validate_payload','control_target_schema'),
    'workflow_progression.py':('next_actions','event_view','approval_state','merge_permission'),
    'workflow_merge_policy.py':('authorization',),
    'workflow_task_merge_policy.py':('boundary','broad_source','register','reconcile','live','policies','derive'),
    'workflow_autonomy.py':('sweep',),
    'workflow_input_pages.py':('store','read','compact_result','expand_result','bounded_events','mechanical_segments','capacity'),
    'workflow_resumption.py':('admission',),
    'workflow_notification_registry.py':('diagnostics','suppress_stale'),
    'workflow_terminal_observation.py':('terminal','rejected','layers'),
    'workflow_acceptance_obligations.py':('enrich','effective','validate_matrix','closure_review'),
    'workflow_issue_policy.py':('normalized','blocking','auto_repair','validate','review_dispositions','environment_recovered'),
    'workflow_completion.py':('_snapshot','_events','plan','_accepted_sources','_policy','ensure_plan','frozen_target',
        'context','facts','target_current','refresh_status','reconcile','record_test','deliver','complete','historical_failures','verify','next_actions'),
    'workflow_completion_repair.py':('queue_repairs','task_issues'),
    'workflow_completion_contracts.py':('payload_schema',),
    'workflow_completion_reports.py':('_save','validation_receipt','closure_receipt'),
    'workflow_verification_lifecycle.py':('pending','no_rerun_candidate_ids','request',
        'request_from_resolved_decision','apply_resolved_decisions','request_for_candidate','reconcile','facts'),
    'workflow_development_policy.py':('record','enable','default_for_request','policies','derive','facts'),
    'workflow_integration_workspace.py':('select',),
    'workflow_intake.py':('accept','scoped_control'),
    'workflow_user_decisions.py':('answer_source','record','pending','resolve','recover_native_answer'),
    'workflow_supplemental.py':('_context_from_event','source_task','request','queue_repairs','permits_closure','summary','docker','mysql_session','verify'),
    'workflow_final_delivery.py':('git','digest','source','preflight','request','states','summary','next_actions','_record','execute'),
    'workflow_merge.py':('merge_candidate',),
    'workflow_integration_recovery.py':('prepare_pending_base','refresh_undelivered','refresh_base'),
    'workflow_tool_bridge.py':('usage',),
    'workflow_wait.py':('capabilities','repo_observation','review'),
    'workflow_documentation.py':('git','document_bytes','inspect','commit_reviewed','commit'),
    'monitor_queue.py':('coalescing_key','compact','delivery_status'),
    'monitor_notifications.py':('_drain',),
    'role_observer.py':('observe_pool_leases','scan_registry','scan_pool','pool_poll','workflow_activities','_workflow_observation','monitor_sender'),
    'scheduler_observation.py':('_live_receipt','capability','receipt'),
}

# These immutable schema objects are read by hot-replaced functions through
# module globals; replacing the functions alone would leave a live host
# advertising the old payload shape.
CONSTANT_TARGETS={
    'workflow_notification_registry.py':('REGISTRY',),
    'business_ledger.py':('_SCHEMA',),
    'workflow_contracts.py':('EVIDENCE','TASK','PAYLOADS','CONTROL_TARGET'),
    'workflow_reports.py':('PERSONAL_SECTIONS','_BOLD_REPORT_FIELD',
        '_COORDINATOR_SECTION_LIMITS','_PERSONAL_SECTION_LIMITS','_LIVE_PHASE_LABELS'),
}


def activate():
    root=Path(__file__).resolve().parent
    with LOCK:
        modules=list({id(m):m for m in sys.modules.values() if m is not None}.values())
        replacements=[]; hashes={}; constants=[]
        loaded_by_path={}
        target_names={Path(relative).name for relative in TARGETS}
        for module in modules:
            path=getattr(module,'__file__',None)
            if path and Path(path).name in target_names:
                loaded_by_path.setdefault(Path(path).resolve(),[]).append(module)
        for relative,names in TARGETS.items():
            source=root/relative;raw=source.read_bytes();tree=ast.parse(raw,filename=str(source))
            hashes[relative]=hashlib.sha256(raw).hexdigest()
            for module in loaded_by_path.get(source,[]):
                constant_names=CONSTANT_TARGETS.get(relative,())
                if constant_names:
                    staged_namespace=dict(module.__dict__)
                    for node in tree.body:
                        if not isinstance(node,ast.Assign):
                            continue
                        assigned={target.id for target in node.targets if isinstance(target,ast.Name)}
                        if not (assigned & set(constant_names)):
                            continue
                        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),
                             staged_namespace,staged_namespace)
                        for name in assigned & set(constant_names):
                            constants.append((module,name,staged_namespace[name]))
                if relative == 'activity_signals.py':
                    assignment=next(n for n in tree.body if isinstance(n,ast.Assign)
                        and any(isinstance(t,ast.Name) and t.id=='RULE_VERSION' for t in n.targets))
                    constants.append((module,'RULE_VERSION',ast.literal_eval(assignment.value)))
                for dotted in names:
                    body=tree.body;owner=module
                    parts=dotted.split('.')
                    for part in parts:
                        node=next(n for n in body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name==part)
                        body=node.body
                    if len(parts)==2:
                        owner=getattr(module,parts[0])
                    old=getattr(owner,parts[-1],None)
                    staging={}
                    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),module.__dict__,staging)
                    new=staging[parts[-1]];new.__qualname__=dotted
                    replacements.append((module,owner,parts[-1],old,new))
        # Validate every function before publishing any replacement. The only
        # added imported dependency belongs to the stateless observation helper.
        from runtime_common import read_json
        # This added capability is a fixed read-only Git query, not shell access.
        # Update cached allowlists on a live host as well as fresh imports.
        for module in loaded_by_path.get(root/'workflow_runtime.py', []):
            module.READ_TOOLS = module.READ_TOOLS | {'role_repo_state','role_decision_context'}
        for module in loaded_by_path.get(root/'manual_access.py', []):
            module.READONLY_TOOLS = module.READONLY_TOOLS | {'role_repo_state'}
        for module,name,value in constants:
            setattr(module,name,value)
        aliases={}
        for module,owner,name,old,new in replacements:
            if Path(module.__file__).name=='scheduler_observation.py':
                module.__dict__.setdefault('read_json',read_json)
            setattr(owner,name,new)
            if old is not None and owner is module:
                aliases[id(old)]=(old,new)
        # One namespace pass, rather than one pass per function. Plugin loading
        # is bounded by the host; quadratic rebinding used to time out and leave
        # the freshly unloaded plugin without its scheduler hook.
        for imported in modules:
            for alias,value in list(vars(imported).items()):
                replacement=aliases.get(id(value))
                if replacement and value is replacement[0]:
                    vars(imported)[alias]=replacement[1]
        query_modules=loaded_by_path.get(root/'workflow_query.py', [])
        query_names=TARGETS['workflow_query.py']
        query_ready=all(callable(getattr(module,name,None))
            for module in query_modules for name in query_names)
        return {'state':'ACTIVATED','mode':'future_calls_only','process_id':os.getpid(),'rebound':len(replacements),
                'source_sha256':hashes,'business_actions':False,
                'readonly_query_bindings':{
                    'state':('READY' if query_ready else 'INCOMPLETE') if query_modules else 'NOT_LOADED',
                    'module_count':len(query_modules),'functions':list(query_names)}}
