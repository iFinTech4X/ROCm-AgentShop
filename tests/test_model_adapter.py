from copy import deepcopy
import pytest
from app.model_adapter import llama_payload, FORMAT_CONTROL

def test_named_tool_is_forced_without_changing_schema_or_history():
    source = {'tools': [{'type':'function','function':{'name':'Output','parameters':{'type':'object'}}}],
              'tool_choice': {'type':'function','function':{'name':'Output'}},
              'messages':[{'role':'user','content':'最多320元，符合就买'}, {'role':'assistant','content':'草稿'}]}
    old = deepcopy(source); result = llama_payload(source)
    assert source == old
    assert result['tools'] == source['tools'] and result['tool_choice'] == 'required'
    assert result['messages'][:-1] == source['messages']
    assert result['messages'][-1]['content'] == FORMAT_CONTROL

def test_real_tool_result_pairs_are_untouched():
    source = {'messages':[{'role':'assistant','tool_calls':[{'id':'a','type':'function','function':{'name':'quote','arguments':'{}'}}]},
                         {'role':'tool','tool_call_id':'a','content':'{"total_minor":29900}'}], 'tool_choice':'auto'}
    assert llama_payload(source) == source

def test_missing_named_tool_is_not_silently_relaxed():
    with pytest.raises(ValueError):
        llama_payload({'tool_choice':{'type':'function','function':{'name':'unknown'}},'tools':[]})
