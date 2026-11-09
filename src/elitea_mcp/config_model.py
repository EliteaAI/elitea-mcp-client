import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from mcp import StdioServerParameters
from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, ValidationInfo,
                      field_validator, model_validator)
from pydantic_core import PydanticCustomError

from . import config as config_store
from .config import SCHEMA_FILE_NAME, SCHEMA_REFERENCE, write_file_atomically
from .utils.name import sanitize
from .utils.package_updates import DEFAULT_CHECK_INTERVAL_HOURS
from .utils.preflight import PreflightError, normalize_deployment_url, validate_project_id

DEFAULT_TIMEOUT = 30
NOTIFICATION_LIMIT = 250
TEXT_READ_AS_TRUE = frozenset({"false", "no", "off", "0"})
TRANSPORT_ALIASES = {"streamable-http": "http", "streamablehttp": "http", "streamable_http": "http"}
CLOSE_MATCH_CUTOFF = 0.75
SKIPPED = "The server is skipped."

logger = logging.getLogger(__name__)


class Severity(Enum):
    """How much a problem matters: an error stops what it belongs to, a warning and an info never do."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class ConfigProblem:
    severity: Severity
    path: str
    message: str

    def __str__(self):
        return f"{self.severity.value}: {self.path}: {self.message}"


@dataclass
class ConfigReport:
    """Every problem found in a config, and the config to run with, which is None when a problem stops the client."""
    config: Optional[dict]
    problems: list = field(default_factory=list)
    skipped_servers: list = field(default_factory=list)

    def filter_by_severity(self, severity):
        return [problem for problem in self.problems if problem.severity is severity]

    @property
    def errors(self):
        return self.filter_by_severity(Severity.ERROR)

    @property
    def notable(self):
        """The problems a user should see outside `elitea-mcp validate`, which alone also lists infos."""
        return [problem for problem in self.problems if problem.severity is not Severity.INFO]

    def format_problems(self, include_info=False):
        problems = self.problems if include_info else self.notable
        return "\n".join(f"  {problem}" for problem in problems)

    def __str__(self):
        return f"Config problems:\n{self.format_problems()}"

    @property
    def summary(self):
        """One short line for a tray notification, which Windows cuts at 256 characters."""
        return summarize_for_notification(self.errors if self.config is None else self.notable)

    def summarize_skipped_servers(self, total):
        problems = [problem for problem in self.errors if problem.path.startswith("servers.")]
        return summarize_for_notification(problems, f"Skipped {len(self.skipped_servers)} of {total} servers: ")


def summarize_for_notification(problems, prefix=""):
    if not problems:
        return ""
    more = f" (+{len(problems) - 1} more, see Validate Config)" if len(problems) > 1 else ""
    first = f"{prefix}{problems[0].path}: {problems[0].message}"
    if len(first) + len(more) > NOTIFICATION_LIMIT:
        first = first[:NOTIFICATION_LIMIT - len(more) - 3] + "..."
    return first + more


def add_field_problem(info: ValidationInfo, severity, key, message):
    info.context["problems"].append(ConfigProblem(severity, f"{info.context.get('prefix', '')}{key}", message))


def read_flag_and_warn_on_false_text(value, info: ValidationInfo):
    """Keep today's truthiness, so an update changes nothing, but say so when the text means the opposite."""
    if isinstance(value, str) and value.strip().lower() in TEXT_READ_AS_TRUE:
        add_field_problem(info, Severity.WARNING, info.field_name,
               f'"{value}" is text, which this client reads as true. Write false without quotes to turn it off.')
    return bool(value)


def parse_timeout_seconds(value, info: ValidationInfo):
    """Accept what int() accepts, as before; fall back to the default with a warning, and refuse what breaks every call."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        add_field_problem(info, Severity.WARNING, info.field_name,
               f'"{value}" is not a whole number of seconds, so {DEFAULT_TIMEOUT} is used.')
        return DEFAULT_TIMEOUT
    if seconds <= 0:
        raise PydanticCustomError("timeout_not_positive",
                                  "must be more than 0 seconds; every tool call would time out at once")
    return seconds


def parse_project_id(value):
    if value is None or value == "":
        return None
    try:
        return validate_project_id(value)
    except PreflightError:
        raise PydanticCustomError("project_id_not_number", 'must be a number, got "{value}"', {"value": value}) from None


def validate_deployment_url(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    try:
        normalize_deployment_url(value)
    except PreflightError:
        raise PydanticCustomError("deployment_url_invalid",
                                  "must be the address of ELITEA, for example https://dev.elitea.ai") from None
    return value


def treat_null_headers_as_empty(value):
    """Read null as no headers, as the http and sse clients always have."""
    return {} if value is None else value


Truthy = Annotated[bool, BeforeValidator(read_flag_and_warn_on_false_text)]
Seconds = Annotated[int, BeforeValidator(parse_timeout_seconds)]


class StdioServer(StdioServerParameters):
    type: Literal["stdio"] = "stdio"
    stateful: Truthy = Field(False, description="Keep the server process running between tool calls")


class RemoteServer(BaseModel):
    type: Literal["http", "sse"]
    url: str = Field(pattern=r"^https?://", description="The server's endpoint, starting with http:// or https://")
    headers: Annotated[dict[str, str], BeforeValidator(treat_null_headers_as_empty)] = Field(
        default_factory=dict, description="HTTP headers sent with every request")
    stateful: Truthy = Field(False, description="Keep the connection open between tool calls")


SERVER_MODELS = {"stdio": StdioServer, "http": RemoteServer, "sse": RemoteServer}


class ClientConfig(BaseModel):
    """The config.json format; it validates a config only through check_config, which supplies the problem list."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_reference: Optional[str] = Field(None, alias="$schema", description="JSON Schema for editor support")
    deployment_url: Annotated[Optional[str], BeforeValidator(validate_deployment_url)] = Field(
        None, description="Base address of ELITEA, for example https://dev.elitea.ai")
    auth_token: Optional[Any] = Field(None, description="Personal access token from ELITEA Settings → Tokens")
    project_id: Annotated[Optional[int], BeforeValidator(parse_project_id)] = Field(
        None, description="ELITEA project that receives the local MCP servers")
    host: Any = Field("0.0.0.0", description="Address `elitea-mcp run --transport sse` listens on")
    port: Any = Field(8000, description="Port `elitea-mcp run --transport sse` listens on")
    timeout: Seconds = Field(DEFAULT_TIMEOUT, description="Seconds to wait for a tool list or a tool call")
    ssl_verify: Truthy = Field(False, description="Verify the platform's TLS certificate")
    update_check: Truthy = Field(True, description="Check PyPI for new versions of the client")
    update_check_interval_hours: Any = Field(DEFAULT_CHECK_INTERVAL_HOURS,
                                             description="Hours between update checks; 0 or less checks once")
    servers: dict[str, Union[StdioServer, RemoteServer]] = Field(
        default_factory=dict, description="Local MCP servers by name")

    @model_validator(mode="before")
    @classmethod
    def read_mcp_servers_and_flag_unknown_keys(cls, raw, info: ValidationInfo):
        problems = info.context["problems"]
        problems.extend(flag_duplicate_keys(raw, ""))
        problems.extend(flag_unknown_keys(raw, cls, "", ignored=("mcpServers",)))
        if "$schema" not in raw:
            problems.append(ConfigProblem(Severity.INFO, "$schema",
                                          f'add "$schema": "{SCHEMA_REFERENCE}" for autocompletion in editors that read JSON Schema.'))
        if "mcpServers" not in raw:
            return raw
        if raw.get("servers"):
            problems.append(ConfigProblem(Severity.WARNING, "mcpServers",
                                          'is ignored because "servers" is set; move its entries into "servers".'))
            return raw
        problems.append(ConfigProblem(Severity.WARNING, "mcpServers", 'is read as "servers"; rename it to "servers".'))
        return {**raw, "servers": raw["mcpServers"]}

    @field_validator("servers", mode="before")
    @classmethod
    def keep_servers_that_can_run(cls, servers, info: ValidationInfo):
        """Skip a server whose config cannot work, so the others still start."""
        if not isinstance(servers, dict):
            raise PydanticCustomError("servers_not_object",
                                      'must be an object of named servers, for example {"github": {"command": "npx"}}')
        context = info.context
        context["problems"].extend(flag_duplicate_keys(servers, "servers."))
        runnable, chosen_name_for, names_for = {}, {}, {}
        for raw_name, raw in servers.items():
            path = f"servers.{raw_name}"
            name = sanitize(raw_name)
            if not name:
                add_problem(context, Severity.ERROR, path, f"the name must contain a letter. {SKIPPED}")
                context["skipped"].append(raw_name)
                continue
            names_for.setdefault(name, []).append(raw_name)
            server = parse_server(raw, path, context)
            if server is None:
                context["skipped"].append(raw_name)
                continue
            runnable[name] = server
            chosen_name_for[name] = raw_name
        report_name_changes(context, names_for, chosen_name_for)
        return runnable


def add_problem(context, severity, path, message):
    context["problems"].append(ConfigProblem(severity, path, message))


def report_name_changes(context, names_for, chosen_name_for):
    """Name the servers that clash after sanitizing, and which one runs; a plain rename is only an info."""
    for name, raw_names in names_for.items():
        if len(raw_names) > 1:
            listed = " and ".join(f'"{raw_name}"' for raw_name in raw_names)
            chosen = chosen_name_for.get(name)
            outcome = f'only "{chosen}" runs' if chosen else "none of them can run"
            add_problem(context, Severity.WARNING, f"servers.{raw_names[-1]}", f'{listed} all become "{name}"; {outcome}.')
        elif raw_names[0] != name:
            add_problem(context, Severity.INFO, f"servers.{raw_names[0]}", f'is registered on the platform as "{name}".')


def parse_server(raw, path, context):
    """Return the validated server, or None after recording why it cannot run."""
    if not isinstance(raw, dict):
        add_problem(context, Severity.ERROR, path, f'must be an object such as {{"command": "npx"}}. {SKIPPED}')
        return None
    raw_type = raw.get("type")
    if raw_type in (None, ""):
        if "url" in raw and "command" not in raw:
            add_problem(context, Severity.ERROR, path, f'has a "url" but no "type"; add "type": "http" (or "sse"). {SKIPPED}')
            return None
        kind = "stdio"
    else:
        kind = TRANSPORT_ALIASES.get(str(raw_type).lower(), str(raw_type).lower())
    model = SERVER_MODELS.get(kind)
    if model is None:
        add_problem(context, Severity.ERROR, f"{path}.type", f'"{raw_type}" is not a server type; use "stdio", "http" or "sse". {SKIPPED}')
        return None
    context["problems"].extend(flag_duplicate_keys(raw, f"{path}."))
    context["problems"].extend(flag_unknown_keys(raw, model, f"{path}."))
    try:
        return model.model_validate({**raw, "type": kind}, context={**context, "prefix": f"{path}."})
    except ValidationError as error:
        for detail in error.errors():
            add_problem(context, Severity.ERROR, format_problem_path(path, detail["loc"]), f"{explain_validation_error(detail)}. {SKIPPED}")
        return None


def format_problem_path(prefix, loc):
    return ".".join([prefix, *map(str, loc)]) if prefix else ".".join(map(str, loc))


def explain_validation_error(detail):
    """Say what the field needs in the user's terms rather than pydantic's."""
    field_name = detail["loc"][-1] if detail["loc"] else ""
    if detail["type"] == "missing":
        return "is required"
    if detail["type"] == "list_type" and field_name == "args":
        return 'must be a list of strings, for example ["--port", "3000"]'
    if detail["type"] == "dict_type":
        return 'must be an object, for example {"NAME": "value"}'
    if detail["type"] == "string_type":
        return "must be text in double quotes"
    if detail["type"] == "string_pattern_mismatch" and field_name == "url":
        return "must start with http:// or https://"
    return detail["msg"]


def flag_duplicate_keys(obj, prefix):
    for key in getattr(obj, "duplicate_keys", ()):
        yield ConfigProblem(Severity.WARNING, f"{prefix}{key}", "appears more than once; only the last one is used.")


def flag_unknown_keys(raw, model, prefix, ignored=()):
    known = {model_field.alias or name for name, model_field in model.model_fields.items()}
    for key in raw:
        if key in known or key in ignored:
            continue
        suggestion = suggest_known_key(key, known)
        hint = f' Did you mean "{suggestion}"?' if suggestion else ""
        yield ConfigProblem(Severity.WARNING, f"{prefix}{key}", f"is not a setting this client reads, so it is ignored.{hint}")


def suggest_known_key(key, known):
    """Compare ignoring case, dashes and underscores, so projectId finds project_id and small typos still match."""
    by_normalized = {normalize_key(name): name for name in known}
    match = difflib.get_close_matches(normalize_key(key), list(by_normalized), n=1, cutoff=CLOSE_MATCH_CUTOFF)
    return by_normalized[match[0]] if match else None


def normalize_key(key):
    return re.sub(r"[-_\s]", "", key).lower()


def check_config(raw):
    """Report every problem in a parsed config at once and return the config the client can run with."""
    context = {"problems": [], "skipped": []}
    try:
        model = ClientConfig.model_validate(raw, context=context)
    except ValidationError as error:
        blockers = [ConfigProblem(Severity.ERROR, format_problem_path("", detail["loc"]), explain_validation_error(detail)) for detail in error.errors()]
        return ConfigReport(None, blockers + context["problems"], context["skipped"])
    return ConfigReport(model.model_dump(by_alias=True), context["problems"], context["skipped"])


def write_config_schema():
    """Write the schema of this client version next to config.json, so "$schema" always matches what is installed."""
    path = config_store.get_config_file().parent / SCHEMA_FILE_NAME
    text = json.dumps(ClientConfig.model_json_schema(by_alias=True), indent=2)
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            write_file_atomically(path, text)
    except OSError as error:
        logger.warning(f"Could not write {path}: {error}")
    return path
