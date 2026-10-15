import json, os, sys, tempfile, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import yaml

SOURCE=Path(__file__).resolve().parents[1]
HERMES=Path.home()/'.hermes/hermes-agent'
sys.path.insert(0,str(HERMES));sys.path.insert(0,str(SOURCE))
requests=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send_json(self,value):
  body=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def do_GET(self):
  if self.path=='/runs':return self.send_json([])
  if self.path=='/metrics':
   body=b'vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n';self.send_response(200);self.end_headers();self.wfile.write(body);return
  return self.send_json({'object':'list','data':[{'id':'local-model','object':'model','context_length':65536},{'id':'cloud-model','object':'model','context_length':65536}]})
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append((self.path,body))
  if self.path=='/local/v1/chat/completions':
   if len([r for r in requests if r[0].startswith('/local/')])==1:
    from runtime_common import save_json
    save_json(home/'runtime/model-routing-state.json',{'mode':'cloud'})
    message={'role':'assistant','content':None,'tool_calls':[{'id':'call_route','type':'function','function':{'name':'terminal','arguments':json.dumps({'command':'printf routing-tool-ok'})}}]}
    return self.send_json({'id':'chat_1','object':'chat.completion','created':int(time.time()),'model':'local-model','choices':[{'index':0,'message':message,'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}})
   return self.send_json({'id':'chat_return','object':'chat.completion','created':int(time.time()),'model':'local-model','choices':[{'index':0,'message':{'role':'assistant','content':'LOCAL_RETURN_OK'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}})
  if self.path=='/cloud/v1/responses':
   response={'id':'resp_1','object':'response','created_at':int(time.time()),'model':'cloud-model','status':'completed','output':[{'id':'msg_1','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'ROUTING_OK','annotations':[]}]}],'usage':{'input_tokens':12,'output_tokens':3,'total_tokens':15}}
   if body.get('stream'):
    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
    events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}}, {'type':'response.output_item.added','output_index':0,'item':{**response['output'][0],'content':[]}}, {'type':'response.content_part.added','item_id':'msg_1','output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}}, {'type':'response.output_text.delta','item_id':'msg_1','output_index':0,'content_index':0,'delta':'ROUTING_OK'}, {'type':'response.output_text.done','item_id':'msg_1','output_index':0,'content_index':0,'text':'ROUTING_OK'}, {'type':'response.output_item.done','output_index':0,'item':response['output'][0]}, {'type':'response.completed','response':response}]
    for i,event in enumerate(events):
     event['sequence_number']=i;self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n').encode());self.wfile.flush()
    return
   return self.send_json(response)
  self.send_error(404)

with tempfile.TemporaryDirectory(prefix='hermes-routing-e2e-') as tmp:
 home=Path(tmp);os.environ['HERMES_HOME']=tmp
 os.environ['NO_PROXY']='127.0.0.1,localhost'
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
 thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 base=f'http://127.0.0.1:{server.server_port}'
 (home/'plugins').mkdir();(home/'plugins/role-workflow-runtime').symlink_to(SOURCE,target_is_directory=True)
 project=home/'project';project.mkdir()
 (project/'ROLE-BINDINGS.json').write_text(json.dumps({'project_root':str(project),'control':'ACTIVE','roles':{'A':{'chat_id':'test','poll_job_id':'nativejob'}}}))
 (home/'role-projects.json').write_text(json.dumps({'projects':[{'project_root':str(project)}]}))
 (home/'.env').write_text('NUMENUME_API_KEY=offline-test-key\n')
 config={'model':{'provider':'qwen-a100','default':'local-model','base_url':base+'/local/v1','api_mode':'chat_completions','context_length':65536},'providers':{'qwen-a100':{'base_url':base+'/local/v1','default_model':'local-model','api_mode':'chat_completions','api_key':'offline-local-key','context_length':65536},'numenume-cloud':{'base_url':base+'/cloud/v1','default_model':'cloud-model','api_mode':'responses','key_env':'NUMENUME_API_KEY','context_length':65536,'extra_body':{'store':False}}},'agent':{'api_max_retries':1},'terminal':{'backend':'local','cwd':str(project)},'memory':{'memory_enabled':False},'compression':{'enabled':False},'plugins':{'enabled':['role-workflow-runtime'],'entries':{'role-workflow-runtime':{'settings':{'metrics_url':base+'/metrics','model_routing':{'enabled':True,'local_model':'local-model','cloud_model':'cloud-model','pipeline_url':base+'/runs'}}}}}}
 (home/'config.yaml').write_text(yaml.safe_dump(config))
 from runtime_common import save_json
 save_json(home/'runtime/model-routing-state.json',{'mode':'local'})
 from hermes_cli.plugins import discover_plugins
 discover_plugins(force=True)
 from run_agent import AIAgent
 agent=AIAgent(model='local-model',provider='qwen-a100',base_url=base+'/local/v1',api_key='offline-local-key',api_mode='chat_completions',enabled_toolsets=['terminal'],quiet_mode=True,skip_memory=True,skip_background_review=True,skip_context_files=True,session_id='cron_nativejob_20261015_120000',platform='feishu',max_iterations=3,max_tokens=64,cwd=str(project))
 agent._disable_streaming=True
 result=agent.run_conversation('Print routing-tool-ok once, then answer.',system_message='Stable system prefix for route verification.')
 assert result.get('final_response')=='ROUTING_OK',str(result)[:1500]
 inference=[r for r in requests if r[0].endswith(('/chat/completions','/responses'))]
 assert [r[0] for r in inference]==['/local/v1/chat/completions','/cloud/v1/responses'],[r[0] for r in inference]
 cloud=inference[1][1]
 assert cloud['store'] is False
 outputs=[x for x in cloud['input'] if x.get('type')=='function_call_output']
 assert len(outputs)==1,cloud['input']
 assert 'routing-tool-ok' in outputs[0]['output'],outputs
 local_system='\n'.join(x['content'] for x in inference[0][1]['messages'] if x['role']=='system')
 assert cloud['instructions']==local_system,'system prefix changed during switch'
 # A second turn uses the same conversation, now exercises native Responses streaming.
 agent._disable_streaming=False
 second=agent.run_conversation('Answer again.',conversation_history=result['messages'])
 assert second.get('final_response')=='ROUTING_OK',str(second)[:1000]
 assert requests[-1][1]['stream'] is True
 # Reverse switch uses Chat Completions and must not leak cloud's store override.
 save_json(home/'runtime/model-routing-state.json',{'mode':'local'})
 agent._disable_streaming=True
 third=agent.run_conversation('Return to local.',conversation_history=second['messages'])
 assert third.get('final_response')=='LOCAL_RETURN_OK',str(third)[:1000]
 assert requests[-1][0]=='/local/v1/chat/completions'
 assert 'store' not in requests[-1][1]
 server.shutdown();server.server_close()
 print('PASS: local -> cloud during tool loop, one real terminal execution, stable system prefix, Responses store=false, Responses streaming, cloud -> local without leaked overrides')
 print('INFERENCE_ROUTES', [r[0] for r in requests if r[0].endswith(('/chat/completions','/responses'))])
