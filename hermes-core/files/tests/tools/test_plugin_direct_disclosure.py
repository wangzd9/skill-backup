"""Plugin disclosure affects presentation, never selection or scoped dispatch."""
import json

from tools.registry import registry
from tools.tool_search import classify_tools, is_deferrable_tool_name


def test_plugin_can_keep_its_command_direct_without_promoting_to_core(tmp_path):
    name='isolated_direct_command'
    schema={'name':name,'description':'isolated session command',
            'parameters':{'type':'object','properties':{}}}
    scope=str(tmp_path/'profile-a')
    import yaml
    from hermes_constants import set_hermes_home_override,reset_hermes_home_override
    from hermes_cli.plugins import PluginManager
    home=tmp_path/'profile-a'; plugin=home/'plugins/direct-test';plugin.mkdir(parents=True)
    (plugin/'plugin.yaml').write_text(yaml.safe_dump({'name':'direct-test','version':'0.1.0','description':'isolated'}))
    (plugin/'__init__.py').write_text('import json\ndef handle(args,**kw): return json.dumps({"ok":True})\n'
        'def register(ctx):\n ctx.register_tool('+repr(name)+',"isolated_session",'+repr(schema)+
        ',handle,disclosure="direct")\n')
    (home/'config.yaml').write_text(yaml.safe_dump({'plugins':{'enabled':['direct-test']}}))
    bound=set_hermes_home_override(home)
    manager=PluginManager();manager.discover_and_load()
    try:
        entry=registry.get_entry(name,scope=scope)
        assert entry.disclosure=='direct'
        assert 'disclosure' not in entry.schema
        assert registry.get_entry(name,scope=str(tmp_path/'profile-b')) is None
        # Classifier uses the caller's actual registry scope, not an env flag.
        token=set_hermes_home_override(scope)
        try:
            assert not is_deferrable_tool_name(name)
            assert is_deferrable_tool_name(name,frozenset({name}))
            visible,deferred=classify_tools([{'type':'function','function':schema}])
            assert visible and not deferred
        finally:
            reset_hermes_home_override(token)
    finally:
        registry.deregister(name,scope=scope)
        reset_hermes_home_override(bound)


def test_default_plugin_still_defers_and_invalid_disclosure_never_registers():
    import pytest
    name='isolated_default_command'
    schema={'name':name,'parameters':{'type':'object','properties':{}}}
    registry.register(name=name,toolset='isolated_optional',schema=schema,handler=lambda args:'{}')
    try:
        assert is_deferrable_tool_name(name)
        with pytest.raises(ValueError,match='disclosure'):
            registry.register(name='invalid_disclosure',toolset='isolated',schema=schema,
                              handler=lambda args:'{}',disclosure='ambient')
        assert registry.get_entry('invalid_disclosure') is None
    finally:
        registry.deregister(name)


def test_activation_accepts_a_live_registry_with_legacy_slotted_entries(monkeypatch):
    import dataclasses
    import importlib
    module=importlib.import_module('tools.registry')
    from agent.native_policy_activation import activate_native_turn_policy
    # Model a gateway started before disclosure was added, including real slots.
    fields=[(field.name,field.type,dataclasses.field(default=field.default))
            if field.default is not dataclasses.MISSING else (field.name,field.type)
            for field in dataclasses.fields(module.ToolEntry) if field.name!='disclosure']
    legacy=dataclasses.make_dataclass('ToolEntry',fields,slots=True,eq=False,
                                   namespace={'__module__':module.__name__})
    monkeypatch.setattr(module,'ToolEntry',legacy)
    schema={'name':'legacy_entry','parameters':{'type':'object','properties':{}}}
    old=legacy('legacy_entry','isolated',schema,lambda args:'{}',None,[],False,'','')
    module.registry._tools['legacy_entry']=old
    try:
        receipt=activate_native_turn_policy()
        assert receipt['state']=='ACTIVATED'
        assert module.registry.get_entry('legacy_entry') is old
        assert not hasattr(old,'disclosure')
        module.registry.register(name='new_direct_after_activation',toolset='isolated',
                                schema=schema,handler=lambda args:'{}',disclosure='direct')
        assert module.registry.get_entry('new_direct_after_activation').disclosure=='direct'
    finally:
        module.registry.deregister('legacy_entry')
        module.registry.deregister('new_direct_after_activation')
