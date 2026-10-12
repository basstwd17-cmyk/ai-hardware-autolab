"""Scope wiring declarations; instrument settings remain ordinary capture args."""
import re


def validate_connections(value):
    if not isinstance(value, (list, tuple)) or len(value) > 4:
        raise ValueError("scope_connections must contain at most four channels")
    rows, seen = [], set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"channel", "signal"}
                or type(row["channel"]) is not int or row["channel"] not in range(1, 5)
                or row["channel"] in seen or not isinstance(row["signal"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.+\-]{1,64}", row["signal"])):
            raise ValueError("scope_connections needs unique CH1..4 and signal names")
        seen.add(row["channel"])
        rows.append(dict(row))
    return rows


def is_connection_confirmation(message):
    # Only an explicit completion of the pending physical request is evidence.
    # Generic yes/no, questions, and model-authored summaries cannot change wiring.
    text = re.sub(r"\s+", "", message.strip().lower()).rstrip(".!。")
    return text in {"연결완료", "연결완료했습니다", "연결했어", "연결했습니다", "connected"}


def scenario_connections(text):
    """Read explicit user channel assignments, never infer wiring from a goal."""
    signal = r"([A-Za-z][A-Za-z0-9_.+\-]{0,63})"
    forward = re.compile(r"\bCH([1-4])\s*(?:[:=←→]\s*|(?:에는|에서는|으로는|로는|에서|으로|은|는|에|로)\s*|\s+)" + signal, re.I)
    reverse = re.compile(r"\b" + signal + r"\s*(?:[:=←→]\s*|(?:은|는|을|를)\s*|\s+)CH([1-4])(?=$|[^A-Za-z0-9_])", re.I)
    rows = {}
    text = re.sub(r"(?:오실로스코프|스코프)(?:의)?\s*", "", text)
    for line in re.split(r"[\n;。]|(?<=[A-Za-z0-9])\.(?=\s)", text):
        # An example, conditional, negation or question is not a declaration.
        if re.search(r"아니|말고|미연결|예시|연결하면|연결\s*안|연결(?:되어|돼|하지|된)?\s*않|\?|\b(?:example|if|unless|not)\b", line, re.I):
            continue
        direct = list(forward.finditer(line))
        matches = [(m.start(), int(m[1]), m[2]) for m in direct]
        matches += [(m.start(), int(m[2]), m[1]) for m in reverse.finditer(line)
                    if not any(m.start() < d.end() and d.start() < m.end() for d in direct)]
        for _, channel, name in sorted(matches):
            name = name.rstrip('.').upper()
            if re.fullmatch(r"CH[1-4]", name):
                continue
            if channel in rows and rows[channel]['signal'] != name:
                raise ValueError(f"CH{channel} has conflicting scenario signal assignments")
            rows[channel] = {'channel': channel, 'signal': name}
    return validate_connections(list(rows.values()))


def declared_connections(events):
    rows = {}
    for event in events:
        if event.kind in {"conversation.scope.connected", "conversation.scope.configured"} and event.agent == "human":
            for row in validate_connections(event.payload["connections"]):
                rows[row["channel"]] = row
    return list(rows.values())
