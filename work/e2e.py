import base64,json,os,subprocess,sys,time,threading,sqlite3,urllib.request,urllib.error,hashlib
from http.server import BaseHTTPRequestHandler,HTTPServer
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from jwt.algorithms import RSAAlgorithm
import asyncio,aiohttp

key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
for filename in ('work/e2e.sqlite3','work/e2e.jsonl'):
 try: os.unlink(filename)
 except FileNotFoundError: pass
jwk=json.loads(RSAAlgorithm.to_jwk(key.public_key())); jwk.update({'kid':'test-key','use':'sig','alg':'RS256'})
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  body=json.dumps({'keys':[jwk]}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*a):pass
jwks=HTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=jwks.serve_forever,daemon=True).start()
env=os.environ.copy();env.update({'MCP_OIDC_ISSUER':'https://issuer.test/','MCP_OIDC_AUDIENCE':'gateway','MCP_OIDC_JWKS_URI':f'http://127.0.0.1:{jwks.server_port}/jwks','MCP_ROLE_MAP':'{}','MCP_GATEWAY_DB':'work/e2e.sqlite3','MCP_GATEWAY_AUDIT':'work/e2e.jsonl','MCP_GATEWAY_HOST':'127.0.0.1','MCP_GATEWAY_PORT':'18081','MCP_SSE_KEEPALIVE_SECONDS':'0.1','MCP_ALLOW_IN_MEMORY_SESSIONS':'true','FEDERATION_TOKEN':'upstream-test-token','MCP_AUDIT_WEBHOOK_URL':'http://127.0.0.1:8001/services/collector/event','MCP_SPLUNK_HEC_TOKEN':'test-hec-token','OTEL_EXPORTER_OTLP_ENDPOINT':'http://127.0.0.1:8001','OTEL_BSP_SCHEDULE_DELAY':'100','OTEL_METRIC_EXPORT_INTERVAL':'250'})
ref=subprocess.Popen([sys.executable,'work/reference_server.py'],env=env,stdout=open('work/e2e-ref.log','w'),stderr=subprocess.STDOUT)
adapter=subprocess.Popen([sys.executable,'work/custom_adapter.py'],env=env,stdout=open('work/e2e-adapter.log','w'),stderr=subprocess.STDOUT)
gw=subprocess.Popen([sys.executable,'main.py'],env=env,stdout=open('work/e2e-gateway.log','w'),stderr=subprocess.STDOUT)
token=jwt.encode({'iss':'https://issuer.test/','aud':'gateway','sub':'tester','exp':int(time.time())+600,'roles':['mcp.admin','mcp.engineer'],'sid':'session-123','model':'model-v1'},key,algorithm='RS256',headers={'kid':'test-key'})
engineer_token=jwt.encode({'iss':'https://issuer.test/','aud':'gateway','sub':'tester','exp':int(time.time())+600,'roles':['mcp.engineer'],'sid':'session-123','model':'model-v1'},key,algorithm='RS256',headers={'kid':'test-key'})
def call(method,url='http://127.0.0.1:18081/mcp',payload=None,extra={},bearer=token):
 headers={'Authorization':'Bearer '+bearer,'Content-Type':'application/json','Accept':'application/json, text/event-stream',**extra}
 req=urllib.request.Request(url,data=json.dumps(payload).encode() if payload is not None else None,headers=headers,method=method)
 try:
  with urllib.request.urlopen(req,timeout=12) as r:return r.status,dict(r.headers),r.read().decode()
 except urllib.error.HTTPError as e:return e.code,dict(e.headers),e.read().decode()
try:
 for _ in range(60):
  try:
   if call('GET','http://127.0.0.1:18081/health')[0]==200:break
  except Exception:pass
  time.sleep(.25)
 status,_,body=call('POST','http://127.0.0.1:18081/tools',{'name':'echo','endpoint':'http://127.0.0.1:8000/mcp','transport':'streamable-http','read_roles':['mcp.engineer'],'write_roles':['mcp.admin'],'tool':{'description':'Echo text','inputSchema':{'type':'object'}}})
 print('register',status,body)
 time.sleep(.5)
 print('register SSE adapter',call('POST','http://127.0.0.1:18081/tools',{'name':'events','transport':'sse','endpoint':'http://127.0.0.1:8001/events','roles':['mcp.engineer'],'tool':{'description':'events'}})[0])
 print('register WebSocket adapter',call('POST','http://127.0.0.1:18081/tools',{'name':'socket','transport':'websocket','endpoint':'ws://127.0.0.1:8001/socket','roles':['mcp.engineer'],'tool':{'description':'socket'}})[0])
 openapi_spec={'openapi':'3.0.3','paths':{'/legacy/users/{user_id}':{'get':{'operationId':'getUser','summary':'Get a legacy user','parameters':[{'name':'user_id','in':'path','required':True,'schema':{'type':'string'}}]}}}}
 api_status,_,api_body=call('POST','http://127.0.0.1:18081/adapters/openapi',{'name':'legacy','base_url':'http://127.0.0.1:8001','spec':openapi_spec,'read_roles':['mcp.engineer'],'write_roles':['mcp.admin']})
 print('OpenAPI adapter discovery',api_status,api_body)
 grpc_status,_,grpc_body=call('POST','http://127.0.0.1:18081/adapters/grpc',{'name':'ledger','target':'127.0.0.1:8003','tls':False,'read_roles':['mcp.engineer'],'write_roles':['mcp.admin']})
 print('gRPC reflection adapter discovery',grpc_status,grpc_body)
 fed_status,_,fed_body=call('POST','http://127.0.0.1:18081/federations',{'name':'corp','tools_url':'http://127.0.0.1:8001/registry/tools','mcp_url':'http://127.0.0.1:8001/registry/mcp','token_env':'FEDERATION_TOKEN','read_roles':['mcp.engineer'],'write_roles':['mcp.admin']})
 print('federation sync',fed_status,fed_body)
 peer_status,_,peer_body=call('POST','http://127.0.0.1:18081/peers',{'name':'partner','registry_url':'http://127.0.0.1:8001/registry/tools','mcp_url':'http://127.0.0.1:8001/registry/mcp','credential_env':'FEDERATION_TOKEN','read_roles':['mcp.engineer'],'write_roles':['mcp.admin']})
 print('peer trust link discovery',peer_status,peer_body)
 sse=urllib.request.Request('http://127.0.0.1:18081/sse/events',headers={'Authorization':'Bearer '+token,'Accept':'text/event-stream'})
 with urllib.request.urlopen(sse,timeout=5) as response:
  print('SSE keepalive received',response.readline().decode().strip()==': gateway keepalive')
 async def websocket_check():
  async with aiohttp.ClientSession() as session:
   async with session.ws_connect('http://127.0.0.1:18081/ws/socket',headers={'Authorization':'Bearer '+token}) as ws:
    await ws.send_str('gateway websocket ping')
    message=await ws.receive(timeout=5)
    return message.data=='gateway websocket ping'
 print('WebSocket proxy echo',asyncio.run(websocket_check()))
 init={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'integration','version':'1'}}}
 status,headers,body=call('POST',payload=init);print('initialize',status,headers.get('Mcp-Session-Id'),body[:300]);sid=headers.get('Mcp-Session-Id')
 if status==200 and sid:
  print('initialized notification',call('POST',payload={'jsonrpc':'2.0','method':'notifications/initialized'},extra={'Mcp-Session-Id':sid,'MCP-Protocol-Version':'2025-03-26'})[0])
  listing=call('POST',payload={'jsonrpc':'2.0','id':2,'method':'tools/list'},extra={'Mcp-Session-Id':sid,'MCP-Protocol-Version':'2025-03-26'})
  print('tools/list exposes MCP, federated, REST, and gRPC tools',listing[0]==200 and all(x in listing[2] for x in ('corp.multiply','partner.multiply','echo','legacy.getUser','ledger.legacy.Calculator.Multiply')))
  denied=call('POST',payload={'jsonrpc':'2.0','id':20,'method':'tools/call','params':{'name':'echo','arguments':{'text':'audit payload'}}},extra={'Mcp-Session-Id':sid},bearer=engineer_token)
  print('read role cannot execute write protected tool',denied[0]==200 and json.loads(denied[2])['error']['code']==-32003)
  call_status,_,call_body=call('POST',payload={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'echo','arguments':{'text':'audit payload'}}},extra={'Mcp-Session-Id':sid,'MCP-Protocol-Version':'2025-03-26'})
  print('tools/call HTTP status',call_status,'valid JSON-RPC SSE result',json.loads(next(x[6:] for x in call_body.splitlines() if x.startswith('data: ')))['result']['structuredContent']['result']=='audit payload')
  remote_status,_,remote_body=call('POST',payload={'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'corp.multiply','arguments':{'a':6,'b':7}}},extra={'Mcp-Session-Id':sid})
  print('federated MCP tool call',remote_status,json.loads(remote_body)['result']['structuredContent']['result']==42)
  peer_status,_,peer_result=call('POST',payload={'jsonrpc':'2.0','id':8,'method':'tools/call','params':{'name':'partner.multiply','arguments':{'a':3,'b':14}}},extra={'Mcp-Session-Id':sid})
  print('peer-to-peer MCP tool call',peer_status,json.loads(peer_result)['result']['structuredContent']['result']==42)
  rest_status,_,rest_body=call('POST',payload={'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'legacy.getUser','arguments':{'path_params':{'user_id':'u-1'}}}},extra={'Mcp-Session-Id':sid})
  rest_result=json.loads(rest_body)['result']['structuredContent']
  print('OpenAPI REST virtual tool and output DLP',rest_status,rest_result=={'status':200,'body':{'userId':'u-1','email':'[REDACTED:EMAIL]'}})
  grpc_status,_,grpc_result=call('POST',payload={'jsonrpc':'2.0','id':6,'method':'tools/call','params':{'name':'ledger.legacy.Calculator.Multiply','arguments':{'a':6,'b':7}}},extra={'Mcp-Session-Id':sid})
  print('gRPC reflected tool execution',grpc_status,json.loads(grpc_result)['result']['structuredContent']=={'value':42})
  pii_status,_,pii_body=call('POST',payload={'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'echo','arguments':{'text':'contact person@example.com'}}},extra={'Mcp-Session-Id':sid})
  pii_result=json.loads(next(x[6:] for x in pii_body.splitlines() if x.startswith('data: ')))['result']['structuredContent']['result']
  print('MCP input and output PII redaction',pii_status,pii_result=='contact [REDACTED:EMAIL]')
 conn=sqlite3.connect('work/e2e.sqlite3');row=conn.execute("select user,session,model,payload_json,payload_sha256,execution_id,execution_sha256,verdict from audit_events where tool='echo' and verdict='allow'").fetchone()
 expected=hashlib.sha256(json.dumps({'text':'audit payload'},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
 print('SQLite audit row verified',row[0:3]==('tester','session-123','model-v1') and row[3]=='{"text":"audit payload"}' and row[4]==expected and len(row[5])==32 and row[6]!=row[4] and row[7]=='allow')
 sinks={'hec': [], 'otlp': []}
 for _ in range(30):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8001/observability',timeout=2) as response: sinks=json.loads(response.read())
   hec_hashes=[item.get('event',{}).get('execution_sha256') for item in sinks['hec']]
   if row[6] in hec_hashes and '/v1/traces' in sinks['otlp']: break
  except Exception: pass
  time.sleep(.25)
 hec_fingerprints=[item.get('event',{}).get('execution_sha256') for item in sinks['hec']]
 print('Splunk HEC received audit fingerprints',row[6] in hec_fingerprints)
 print('OTLP trace exporter reached collector mock','/v1/traces' in sinks['otlp'])
finally:
 for process in (gw,ref,adapter): process.terminate()
 for process in (gw,ref,adapter):
  try: process.wait(timeout=7)
  except subprocess.TimeoutExpired: process.kill();process.wait(timeout=3)
 jwks.shutdown()
