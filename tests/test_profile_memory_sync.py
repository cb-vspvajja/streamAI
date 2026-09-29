from types import SimpleNamespace
from copy import deepcopy
import asyncio
import threading
from unittest.mock import Mock,AsyncMock
import pytest
from agentmemory import NotFoundError
from ui.app.profile_memory_sync import reconcile,SOURCE
from ui.app.memory_service import AgentMemoryService,DemoState

class Session:
    end_time=None
    def __init__(self):self.blocks=[];self.adds=[];self.deletes=[];self.updates=[];self.offsets=[]
    def list_memories(self,limit,offset):
        self.offsets.append(offset)
        return SimpleNamespace(blocks=deepcopy(self.blocks[offset:offset+limit]),total=len(self.blocks))
    def add_memory(self,**kwargs):
        self.adds.append(kwargs);ids=[]
        for fact in kwargs['facts']:
            key='b'+str(len(self.blocks)+1);ids.append(key)
            self.blocks.append({'block_id':key,'fact':fact,'annotations':kwargs['annotations'],'status':'processing'})
        return SimpleNamespace(block_ids=ids,accepted_count=len(ids),rejected_count=0)
    def delete_memory(self,block_ids):
        self.deletes.extend(block_ids);self.blocks=[b for b in self.blocks if b['block_id'] not in block_ids]
        return SimpleNamespace(deleted_count=len(block_ids))
    def update_memory(self,**kwargs):self.updates.append(kwargs)

class User:
    def __init__(self):self.session=None;self.created=[]
    def get_session(self,session_id):
        if self.session is None:raise NotFoundError('missing')
        return self.session
    def create_session(self,**kwargs):self.created.append(kwargs);self.session=Session();return self.session

def sync(user,facts,apply=True):
    return reconcile(user,'viewer-sai',facts,apply=apply,blocks_from_result=lambda p:p.blocks,is_ready=AgentMemoryService._block_is_ready)

def block(fact,source=SOURCE,status='ready',key='b1'):
    return {'block_id':key,'fact':fact,'annotations':{'source':source},'status':status}


def test_seeded_preferences_are_submitted_once_and_ready_is_separate():
    user=User();facts=['Viewer prefers Comedy content.','Viewer dislikes Horror content.']
    result,_=sync(user,facts)
    assert result['status']=='pending' and result['acceptedFacts']==2 and result['readyFacts']==0
    assert user.created[0]['session_id'].startswith('streamai-profile-')
    assert user.session.adds[0]['memory_block_ttl']==0 and user.session.adds[0]['async_processing']
    result,_=sync(user,facts)
    assert len(user.session.adds)==1 and result['storedFacts']==2
    for b in user.session.blocks:b['status']='ready'
    assert sync(user,facts)[0]['status']=='ready'


def test_repeated_login_without_local_state_does_not_duplicate():
    user=User();sync(user,['a']);sync(user,['a'])
    assert len(user.created)==1 and len(user.session.adds)==1


def test_change_removes_only_managed_old_preferences():
    user=User();user.session=Session();user.session.blocks=[block('old'),block('historic conversation','chat',key='history')]
    result,_=sync(user,['new'])
    assert result['removedFacts']==1
    assert user.session.deletes==['b1']
    assert any(b['block_id']=='history' for b in user.session.blocks)


def test_clearing_preferences_removes_managed_mirror():
    user=User();sync(user,['a']);result,_=sync(user,[])
    assert result['status']=='empty' and result['removedFacts']==1


def test_inspection_never_creates_or_updates_memory():
    user=User();assert sync(user,['a'],False)[0]['status']=='not_synced';assert not user.created
    sync(user,['old']);result,_=sync(user,['new'],False)
    assert result['status']=='out_of_sync' and len(user.session.adds)==1 and not user.session.deletes


def test_partial_acceptance_does_not_remove_previous_mirror():
    user=User();user.session=Session();user.session.blocks=[block('old')]
    user.session.add_memory=lambda **kw:SimpleNamespace(block_ids=[],accepted_count=0,rejected_count=1)
    with pytest.raises(RuntimeError,match='only part'):sync(user,['new'])
    assert not user.session.deletes


def test_failed_enrichment_is_retried_without_adding_duplicate():
    user=User();user.session=Session();user.session.blocks=[block('a',status='extraction_failed')]
    assert sync(user,['a'],False)[0]['status']=='enrichment_failed'
    result,_=sync(user,['a'])
    assert result['retriedFacts']==1 and not user.session.adds
    assert result['status']=='pending'
    assert not AgentMemoryService._block_is_ready({'status':'extraction_failed','ingested_at':'yesterday'})


def test_pagination_prevents_duplicates_beyond_first_page():
    user=User();user.session=Session();user.session.blocks=[block(str(n),key=str(n)) for n in range(205)]
    result,_=sync(user,[str(n) for n in range(205)])
    assert result['storedFacts']==205 and user.session.offsets==[0,200] and not user.session.adds


def test_sync_errors_are_visible_and_operational_state_is_preserved():
    memory=AgentMemoryService.__new__(AgentMemoryService);memory._lock=threading.RLock()
    memory.state=DemoState(user_id='u',user=SimpleNamespace(get_session=Mock(side_effect=RuntimeError('offline'))))
    memory._save_state=Mock()
    result=memory.sync_operational_profile(['a'])
    assert result['status']=='error' and result['error']=='RuntimeError'


def test_login_and_profile_edit_hooks_sync_current_preferences(monkeypatch):
    import ui.app.main as main
    memory=Mock();memory.login.return_value={'user_name':'Sai'};memory.snapshot.return_value={'login_id':'sai'}
    memory.sync_operational_profile.return_value={'status':'pending','expectedFacts':1}
    catalogue=Mock();catalogue.profile_memory_blocks.return_value=[{'fact':'a'}]
    monkeypatch.setattr(main,'_services_wait',AsyncMock(return_value=(memory,None,catalogue)))
    result=asyncio.run(main.login_viewer(main.LoginRequest(login_id='sai',pin='1234')))
    memory.sync_operational_profile.assert_called_once_with(['a'],apply=True,expected_login_id='sai')
    assert result['data']['profile_memory_sync']['status']=='pending'


def test_profile_inspection_hook_does_not_write(monkeypatch):
    import ui.app.main as main
    memory=Mock();memory.snapshot.return_value={'login_id':'sai'};memory.sync_operational_profile.return_value={'status':'ready'}
    catalogue=Mock();catalogue.profile_memory_blocks.return_value=[{'fact':'a'}];catalogue.profile_view.return_value={}
    monkeypatch.setattr(main,'_services_wait',AsyncMock(return_value=(memory,None,catalogue)))
    result=asyncio.run(main.profile())
    memory.sync_operational_profile.assert_called_once_with(['a'],apply=False,expected_login_id='sai')
    assert result['data']['memorySync']['status']=='ready'


def test_installed_sdk_transport_contract_for_sync():
    import httpx,json,uuid
    from agentmemory import AgentMemoryClient,UserResource
    client=AgentMemoryClient('http://memory-test',max_retries=0)
    session_doc=None; blocks=[]; calls=[]
    def transport(request):
        nonlocal session_doc,blocks
        path=request.url.path;calls.append((request.method,path))
        if path.endswith('/sessions') and request.method=='POST':
            body=json.loads(request.content)
            session_doc={'user_id':'viewer-sai','session_id':body['session_id'],'start_time':'2026-09-28T12:00:00Z','end_time':None,'annotations':body['annotations'],'blocks_ttl':body['memory_blocks_ttl']}
            return httpx.Response(201,json=session_doc)
        if '/sessions/' in path and '/memory' not in path and request.method=='GET':
            return httpx.Response(200,json=session_doc) if session_doc else httpx.Response(404,json={'detail':'not found'})
        if path.endswith('/memory') and request.method=='GET':
            assert request.url.params['session_ids']==session_doc['session_id']
            return httpx.Response(200,json={'memory_blocks':blocks,'count':len(blocks),'total':len(blocks),'limit':200,'offset':0})
        if path.endswith('/memory') and request.method=='POST':
            body=json.loads(request.content);ids=[]
            for fact in body['facts']:
                key=str(uuid.uuid4());ids.append(key)
                blocks.append({'block_id':key,'user_id':'viewer-sai','session_id':session_doc['session_id'],'fact':fact,'annotations':body['annotations'],'status':'processing','ingested_at':'2026-09-28T12:00:00Z'})
            return httpx.Response(200,json={'message':'accepted','accepted_count':len(ids),'block_ids':ids,'rejected_count':0})
        if path.endswith('/memory') and request.method=='DELETE':
            body=json.loads(request.content);ids=body['block_ids'];before=len(blocks)
            blocks=[b for b in blocks if b['block_id'] not in ids]
            return httpx.Response(200,json={'deleted_count':before-len(blocks)})
        raise AssertionError((request.method,str(request.url)))
    client._http._client.close()
    client._http._client=httpx.Client(base_url='http://memory-test',transport=httpx.MockTransport(transport))
    user=UserResource(client._http,'viewer-sai','Sai')
    kwargs=dict(apply=True,blocks_from_result=AgentMemoryService._blocks_from_result,is_ready=AgentMemoryService._block_is_ready)
    first,_=reconcile(user,'viewer-sai',['Viewer prefers Comedy content.'],**kwargs)
    second,_=reconcile(user,'viewer-sai',['Viewer prefers Comedy content.'],**kwargs)
    assert first['acceptedFacts']==1 and second['storedFacts']==1 and second['readyFacts']==0
    changed,_=reconcile(user,'viewer-sai',['Viewer dislikes Horror content.'],**kwargs)
    assert changed['removedFacts']==1 and len(blocks)==1
    assert len([c for c in calls if c[0]=='POST' and c[1].endswith('/memory')])==2
    client.close()


def test_seed_and_edit_hooks_sync_without_legacy_duplicate_fact_writes(monkeypatch):
    import ui.app.main as main
    memory=Mock();memory.snapshot.return_value={'login_id':'sai'}
    memory.sync_operational_profile.return_value={'status':'pending','expectedFacts':1}
    catalogue=Mock();catalogue.profile_memory_blocks.return_value=[{'fact':'a'}]
    monkeypatch.setattr(main,'_services_wait',AsyncMock(return_value=(memory,None,catalogue)))
    showcase=Mock();showcase.seed_persona.return_value={'persona':'executive'}
    monkeypatch.setattr(main,'showcase_service',showcase)
    seeded=asyncio.run(main.showcase_persona(main.ShowcasePersonaRequest(persona_id='executive')))
    assert seeded['data']['memorySync']['status']=='pending'
    edited=asyncio.run(main.update_preferences(main.PreferenceUpdateRequest(preferred_genres=['Comedy'])))
    assert edited['memorySync']['status']=='pending'
    assert memory.sync_operational_profile.call_count==2
    memory.record_external_facts.assert_not_called()


def test_background_sync_cannot_write_previous_viewer_preferences_to_new_viewer():
    memory=AgentMemoryService.__new__(AgentMemoryService);memory._lock=threading.RLock()
    memory.state=DemoState(login_id='other',user_id='viewer-other',user=Mock())
    memory._save_state=Mock()
    result=memory.sync_operational_profile(['Sai preference'],expected_login_id='sai')
    assert result['status']=='deferred'
    memory.state.user.get_session.assert_not_called()
    memory._save_state.assert_not_called()


def test_chat_preference_changes_reconcile_mirror_in_background(monkeypatch):
    import ui.app.main as main
    memory=Mock();catalogue=Mock()
    monkeypatch.setattr(main,'showcase_service',None)
    monkeypatch.setattr(main,'background_persistence_lock',asyncio.Lock())
    synchronise=AsyncMock(return_value={'status':'pending'})
    monkeypatch.setattr(main,'_sync_profile_memory',synchronise)
    monkeypatch.setattr('ui.app.usage_reporter.record',Mock())
    asyncio.run(main._persist_chat_artifacts(memory=memory,catalogue=catalogue,login_id='sai',
        target=object(),user_message='I prefer comedy',assistant_message='Saved',facts=['comedy'],
        metric_event={},sync_profile=True))
    synchronise.assert_awaited_once_with(memory,catalogue,'sai')
