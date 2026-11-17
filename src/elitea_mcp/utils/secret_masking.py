"""What counts as a secret in this client's output, and how it is masked: configured values in every form a line may carry them, and URLs cut to what is safe to show."""
import json
import re
import urllib.parse

MASK = "****"
MIN_SECRET_LENGTH = 4


def collect_secret_values(config, server_conf):
    """The configured values a server's output may echo, in every form a log line may carry them: the platform token, env values and header values."""
    env_values = (value for name, value in (server_conf.get("env") or {}).items() if not is_plain_setting(name, value))
    values = [config.get("auth_token"), *env_values, *(server_conf.get("headers") or {}).values()]
    secrets = set()
    for value in values:
        if not isinstance(value, str):
            continue
        secrets.update(list_secret_forms(value))
        scheme, _, credential = value.partition(" ")
        if credential and scheme.lower() in ("bearer", "basic", "token"):
            secrets.update(list_secret_forms(credential))
    return sorted((secret for secret in secrets if len(secret) >= MIN_SECRET_LENGTH), key=len, reverse=True)


def collect_config_secrets(config):
    """Every value a whole config could leak: the token and the env and headers of each server in both server blocks, of any shape."""
    secrets = set(collect_secret_values(config, {}))
    for block in ("servers", "mcpServers"):
        servers = config.get(block)
        for server_conf in servers.values() if isinstance(servers, dict) else ():
            if isinstance(server_conf, dict):
                secrets.update(collect_secret_values(config, keep_object_settings(server_conf)))
    return secrets


def keep_object_settings(server_conf):
    """The env and headers of a server only when they are objects: the config check skips a server whose are not, so it never runs."""
    return {key: server_conf[key] for key in ("env", "headers") if isinstance(server_conf.get(key), dict)}


def is_credential_name(name):
    return any(part in str(name).upper() for part in ("PASS", "SECRET", "TOKEN", "KEY", "PIN", "AUTH", "CRED", "BEARER"))


def is_plain_setting(name, value):
    """A port, a count or a flag under a name that does not suggest a credential, which masking would garble in every number it appears in."""
    return (isinstance(value, str) and re.fullmatch(r"\d{1,5}|true|false", value.strip(), re.IGNORECASE) is not None
            and not is_credential_name(name))


def list_secret_forms(value):
    """A value as written, escaped in JSON (also unescaped Unicode, and JSON inside JSON) or a repr of text or bytes, URL-encoded with or without its slashes, line by line, and as URL masking leaves a value that is itself a URL."""
    return {value, value.strip(), json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1],
            json.dumps(json.dumps(value))[3:-3], repr(value)[1:-1], repr(value.encode("utf-8", "replace"))[2:-1],
            urllib.parse.quote(value, safe=""), urllib.parse.quote(value), urllib.parse.quote_plus(value),
            mask_url_secrets(value), *(line.strip() for line in value.splitlines())}


def mask_secrets(lines, secrets):
    """Replace the values before masking the URLs, so URL masking cannot cut a value apart, except a value that overlaps a URL scheme, such as "https", as replacing it would hide that URL from masking."""
    masked = []
    for line in lines:
        schemes = _find_url_scheme_spans(line)
        replaced_first = [secret for secret in secrets if secret in line and not _touches_a_url_scheme(line, secret, schemes)]
        for secret in replaced_first:
            line = line.replace(secret, MASK)
        line = mask_url_secrets(line)
        for secret in secrets:
            line = line.replace(secret, MASK)
        masked.append(line)
    return masked


def _find_url_scheme_spans(line):
    """The start and end of every scheme with its "://", such as "postgresql://", walking back by hand to stay linear on any input."""
    spans = []
    separator = line.find("://")
    while separator != -1:
        start = separator
        while start and (line[start - 1].isalnum() or line[start - 1] in "_+.-"):
            start -= 1
        spans.append((start, separator + 3))
        separator = line.find("://", separator + 3)
    return spans


def _touches_a_url_scheme(line, secret, scheme_spans):
    """Whether the secret occurs sharing a character with some scheme://, by searching only the window around each one where such an occurrence fits."""
    return any(secret in line[max(start - len(secret) + 1, 0):end + len(secret) - 1] for start, end in scheme_spans)


def redact_urls(text):
    """Keep only the scheme and host of every URL, because hosted MCP servers often carry a secret in the path or query."""
    return drop_url_userinfo(replace_urls(text, reduce_url_to_host, r"https?://[^\s/?#]\S*"))


def mask_url_secrets(text):
    """For text shown on this machine: drop a URL's user and password and mask every query and fragment value."""
    return drop_url_userinfo(replace_urls(text, mask_url, r"https?://\S+"))


def drop_url_userinfo(text):
    """Remove user:password@ from a URL of any scheme, such as a postgresql:// connection string a server prints, up to the last @ before the host as a password may hold one."""
    return re.sub(r"(?<![\w+.-])([a-zA-Z][\w+.-]*://)[^\s/?#]*@", r"\1", text)


def replace_urls(text, rewrite, pattern):
    """Match each URL greedily and give back its trailing punctuation, which keeps the match linear on any input."""
    def rewrite_keeping_trailing_punctuation(match):
        url = match.group(0)
        start = url.index("://") + 3
        kept = url[:start] + (url[start:].rstrip(".,:;)\"'") or url[start])
        return rewrite(kept) + url[len(kept):]
    return re.sub(pattern, rewrite_keeping_trailing_punctuation, text)


def reduce_url_to_host(url):
    match = re.match(r"(https?://)(?:[^\s/@?#]*@)?([^\s/?#]+)", url)
    return f"{match.group(1)}{match.group(2).rpartition('@')[2]}" if match else url


def mask_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return reduce_url_to_host(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path,
                                    mask_parameters(parts.query), mask_parameters(parts.fragment)))


def mask_parameters(text):
    pairs = (part.partition("=") for part in text.split("&") if part)
    return "&".join(f"{key}={MASK}" if separator else MASK for key, separator, _ in pairs)
