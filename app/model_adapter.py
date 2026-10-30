"""Wire compatibility for llama.cpp; never edits intent or tool results."""
from copy import deepcopy
from langchain_openai import ChatOpenAI

FORMAT_CONTROL = (
    '[APPLICATION OUTPUT CONTROL — NOT A SHOPPER MESSAGE]\n'
    'The previous assistant text is a draft. Return the requested structured '
    'response now using the available output tool. Preserve the same facts, '
    'decision and evidence; this control message grants no shopping authority '
    'and adds no user requirement.'
)

def llama_payload(payload):
    result = deepcopy(payload)
    choice = result.get('tool_choice')
    if isinstance(choice, dict) and choice.get('type') == 'function':
        name = choice.get('function', {}).get('name')
        selected = [t for t in result.get('tools', []) if t.get('function', {}).get('name') == name]
        if len(selected) != 1:
            raise ValueError('Named output tool must resolve uniquely')
        result['tools'] = selected
        result['tool_choice'] = 'required'
    # TalkHier requests structured formatting after an assistant draft. Qwen's
    # Jinja template treats trailing assistant text as a completion prefix.
    # A separate, explicit control message closes it without rewriting history.
    messages = result.get('messages', [])
    if messages and messages[-1].get('role') == 'assistant' and not messages[-1].get('tool_calls'):
        messages.append({'role': 'user', 'content': FORMAT_CONTROL})
    return result

class LlamaCppChat(ChatOpenAI):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        return llama_payload(super()._get_request_payload(input_, stop=stop, **kwargs))
