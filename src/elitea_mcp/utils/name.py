import re


def sanitize(name: str) -> str:
    # Remove leading non-letters
    result = re.sub(r'^[^a-zA-Z]+', '', name)
    # Remove trailing non-letters/digits/underscores
    result = re.sub(r'[^a-zA-Z0-9_]+$', '', result)
    # Replace invalid middle characters with _
    return re.sub(r'[^a-zA-Z0-9_]', '_', result)


def build_agent_identifier(agent_name: str):
    return re.sub(r'[^A-Za-z0-9_\-]', "_", agent_name)