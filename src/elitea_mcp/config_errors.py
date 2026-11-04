import json
import re
from enum import Enum
from pathlib import Path

JSON_TOKEN = re.compile(r"""'(?:[^'\\]|\\.)*'?|"(?:[^"\\]|\\.)*"?|[{}\[\]:,]|[^\s{}\[\]:,"']+|\s+""")
VISIBLE_VALUE_KEYS = frozenset({
    "deployment_url", "host", "port", "project_id", "timeout", "ssl_verify", "update_check",
    "update_check_interval_hours", "type", "cwd", "encoding", "encoding_error_handler", "stateful",
})


class ConfigMistake(Enum):
    """A known way to break config.json, each carrying the hint that fixes it."""
    BACKSLASH = 'Backslashes in Windows paths must be doubled ("C:\\\\Users\\\\...") or use forward slashes ("C:/Users/...").'
    TRAILING_COMMA = "Remove the comma after the last item."
    COMMENT = "JSON does not support comments; remove the // or # text."
    SINGLE_QUOTE = "Use double quotes for keys and strings."
    MISSING_COMMA = "Add a comma between items."
    UNCLOSED = "The file ends too early; check that every { and [ has its closing } and ]."
    EMPTY_FILE = "Write {} into it."
    NOT_OBJECT = "The whole file must be one JSON object wrapped in { }."
    NOT_UTF8 = "Save the file with UTF-8 encoding."
    UNKNOWN = "Check the JSON syntax at this position."

    @property
    def hint(self):
        return self.value


class ConfigError(Exception):
    """A config file the client cannot load, described with where the problem is and how to fix it."""

    def __init__(self, path, problem, reason, mistake, line=None, column=None, source_line=""):
        self.path = path
        self.problem = problem
        self.reason = reason
        self.mistake = mistake
        self.line = line
        self.column = column
        self.source_line = source_line
        super().__init__(self._describe())

    @property
    def hint(self):
        return self.mistake.hint

    @property
    def location(self):
        return f"line {self.line}, column {self.column}: " if self.line else ""

    @property
    def summary(self):
        """One short line for a tray notification, which has no room for the source excerpt."""
        return f"{self.location}{self.reason}. {self.hint}"

    def _describe(self):
        lines = [f"{self.problem}: {self.path}", f"  {self.location}{self.reason}"]
        if self.source_line.strip():
            caret_indent = "".join(char if char == "\t" else " " for char in self.source_line[:self.column - 1])
            lines += [f"    {self.source_line}", f"    {caret_indent}^"]
        lines.append(f"Hint: {self.hint}")
        backup = backup_file_for(self.path)
        if backup.exists():
            lines.append(f"The last valid config is saved in {backup}")
        return "\n".join(lines)


def backup_file_for(config_file):
    config_file = Path(str(config_file))
    return config_file.with_name(config_file.name + ".bak")


def parse_config(text, config_file):
    """Return the config object in text, raising ConfigError with the position and a fix hint when it is not one."""
    if not text.strip():
        raise ConfigError(config_file, "Config file is not valid JSON", "The file is empty",
                          ConfigMistake.EMPTY_FILE, line=1, column=1)
    try:
        config = json.loads(text)
    except json.JSONDecodeError as error:
        position, reason, mistake = locate_json_mistake(error)
        line = text.count("\n", 0, position) + 1
        line_start = text.rfind("\n", 0, position) + 1
        line_end = text.find("\n", position)
        source_line = redact_values(text)[line_start:line_end if line_end != -1 else len(text)].rstrip("\r")
        raise ConfigError(config_file, "Config file is not valid JSON", reason, mistake,
                          line=line, column=position - line_start + 1, source_line=source_line) from None
    if not isinstance(config, dict):
        raise ConfigError(config_file, "Config file is not a JSON object",
                          f"It holds a JSON {type(config).__name__} instead", ConfigMistake.NOT_OBJECT)
    return config


def locate_json_mistake(error):
    """Return the position, reason and mistake behind a JSON error; a trailing comma is reported at the comma itself."""
    text, position, reason = error.doc, error.pos, error.msg
    char = text[position:position + 1]
    if char in ("/", "#"):
        return position, reason, ConfigMistake.COMMENT
    if char == "'":
        return position, reason, ConfigMistake.SINGLE_QUOTE
    if "trailing comma" in reason:
        return position, reason, ConfigMistake.TRAILING_COMMA
    if char in ("}", "]") and text[:position].rstrip().endswith(","):
        return text.rfind(",", 0, position), f"Trailing comma before {char}", ConfigMistake.TRAILING_COMMA
    if reason.startswith("Invalid \\escape"):
        return position, reason, ConfigMistake.BACKSLASH
    if not text[position:].strip():
        return position, reason, ConfigMistake.UNCLOSED
    if reason == "Expecting ',' delimiter":
        return position, reason, ConfigMistake.MISSING_COMMA
    return position, reason, ConfigMistake.UNKNOWN


def redact_values(text):
    """Star out every value whose key the client does not know to be harmless, so an unknown secret fails closed; positions never move."""
    tokens = [token for token in JSON_TOKEN.finditer(text) if not token.group().isspace()]
    redacted = list(text)
    owners = []
    key = None
    for index, token in enumerate(tokens):
        value = token.group()
        owner = key if key is not None else owners[-1] if owners else None
        if value in ("[", "{"):
            owners.append(owner if value == "[" else None)
            key = None
        elif value in ("]", "}"):
            if owners:
                owners.pop()
            key = None
        elif value == ",":
            key = None
        elif value == ":":
            continue
        elif index + 1 < len(tokens) and tokens[index + 1].group() == ":" and (
                index == 0 or tokens[index - 1].group() != ":"):
            key = value.strip("\"'")
        else:
            if owner not in VISIBLE_VALUE_KEYS:
                mask_token(redacted, token)
            key = None
    return "".join(redacted)


def mask_token(characters, token):
    """Keep the quotes of a closed string so the excerpt still reads as JSON."""
    start, end = token.span()
    value = token.group()
    if value[0] in "\"'" and len(value) >= 2 and value[-1] == value[0]:
        start, end = start + 1, end - 1
    characters[start:end] = "*" * (end - start)
