from pathlib import Path
import sys
import math
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'usage-gateway'))
from comparison import DEFAULTS,estimate,validate_assumptions


def chat(viewer='sai'):
    return {'kind':'activity','category':'chat_turn','viewerId':viewer}

def request(source='app',operation='chat/completions',inp=100,out=20,status='succeeded'):
    return {'kind':'model_request','source':source,'operation':operation,'status':status,
            'usage':{'inputTokens':inp,'outputTokens':out}}


def test_net_savings_include_background_and_all_viewers():
    rows=[chat(),chat('another'),request(),request('agent_memory',inp=500,out=100),request('ingestion','embeddings',inp=200,out=0)]
    c=estimate(rows)
    assert c['completedChats']==2
    assert c['baseline']['llmTokens']==2400
    assert c['observed']['llmTokens']==720
    assert c['estimatedDifference']['llmTokens']==1680
    assert c['estimatedDifference']['llmRequests']==0
    assert c['observed']['embeddingInputTokens']==200
    assert c['baseline']['referenceCostUsd']==pytest.approx(.009)
    assert c['observed']['referenceCostUsd']==pytest.approx(.00272)
    assert c['estimatedDifference']['referenceCostUsd']==pytest.approx(.00628)
    assert c['basis']=='hypothetical_baseline' and c['provisional']


def test_negative_differences_are_not_clamped():
    c=estimate([chat(),request(inp=2000,out=500),request('agent_memory',inp=3000,out=500)])
    assert c['estimatedDifference']['llmRequests']==-1
    assert c['estimatedDifference']['llmTokens']==-4800
    assert c['llmReductionPct']==-400
    assert c['estimatedDifference']['referenceCostUsd']<0


@pytest.mark.parametrize('record',[request(inp=None),request(status='http_error'),request(status='unknown_outcome'),request(status='in_flight')])
def test_incomplete_usage_blocks_token_and_money_savings(record):
    c=estimate([chat(),record])
    assert c['estimatedDifference']['llmTokens'] is None
    assert c['estimatedDifference']['referenceCostUsd'] is None
    assert c['estimatedDifference']['llmRequests']==0


def test_missing_embedding_usage_blocks_money_but_not_llm_delta():
    c=estimate([chat(),request(),request('agent_memory','embeddings',inp=None,out=0)])
    assert c['estimatedDifference']['llmTokens']==1080
    assert c['estimatedDifference']['embeddingInputTokens'] is None
    assert c['estimatedDifference']['referenceCostUsd'] is None


def test_no_chats_has_no_savings_claim_even_with_setup_usage():
    c=estimate([request('setup')])
    assert all(v is None for v in c['estimatedDifference'].values())
    assert c['observed']['llmRequests']==1


def test_explicit_embedding_baseline_and_rates_are_used():
    assumptions={**DEFAULTS,'embeddingRequestsPerChat':1,'embeddingTokensPerChat':100,'embeddingInputUsdPerMillion':2}
    c=estimate([chat()],assumptions)
    assert c['baseline']['embeddingRequests']==1
    assert c['baseline']['referenceCostUsd']==pytest.approx(.0047)
    assert c['assumptionsOrigin']=='operator_configured'


@pytest.mark.parametrize('change',[{'llmRequestsPerChat':-1},{'llmRequestsPerChat':.5},{'llmInputUsdPerMillion':math.nan},{'embeddingRequestsPerChat':1},{'llmRequestsPerChat':True},{'llmInputUsdPerMillion':'2.5'}])
def test_invalid_assumptions_rejected(change):
    with pytest.raises(ValueError):validate_assumptions({**DEFAULTS,**change})
