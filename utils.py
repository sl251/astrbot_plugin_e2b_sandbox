"""Small general-purpose helpers."""

def shlex_quote(value: str):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"
