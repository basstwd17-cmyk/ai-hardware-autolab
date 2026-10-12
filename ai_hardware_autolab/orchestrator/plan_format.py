"""Bounded syntax repair, never an engineering or authority rewrite."""
import hashlib
import json


class PlanFormatError(ValueError):
    def __init__(self, message, final_text):
        super().__init__(message)
        self.final_text = final_text


def decode_json(text, repairs):
    """Insert at most ONE missing comma across the whole wire and inner JSON.

    No quotes, values, names, signs, braces or authorization fields are changed.
    The candidate must parse completely; ambiguous/truncated text is refused.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        if repairs or len(text.encode()) > 262144 or error.msg != "Expecting ',' delimiter":
            raise
        candidate = text[:error.pos] + ',' + text[error.pos:]
        def unique(pairs):
            result = {}
            for key, val in pairs:
                if key in result:
                    raise error
                result[key] = val
            return result
        value = json.loads(candidate, object_pairs_hook=unique)
        repairs.append({'operation': 'insert_comma', 'offset': error.pos,
                        'before_sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'after_sha256': hashlib.sha256(candidate.encode()).hexdigest()})
        return value


def is_plan_format_reason(reason):
    return reason.startswith('plan_format:') or (
        reason.startswith('runtime_failure: lead-agent could not run while planning:')
        and ('invalid handoff JSON string:' in reason
             or 'final answer that is not one JSON object:' in reason))
