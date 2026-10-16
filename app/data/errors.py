import re


def redact_message(message: str) -> str:
    """Remove common credential material before an error reaches a client/log."""
    value = re.sub(r"AKIA[0-9A-Z]{16}", "[REDACTED_ACCESS_KEY]", message)
    value = re.sub(r"(?i)(aws_secret_access_key|secret_access_key|signature|x-amz-credential)=?[^\s,;]+", r"\1=[REDACTED]", value)
    return value


class DataError(Exception):
    """Base class for safe, user-facing data errors."""


class DataNotFound(DataError):
    pass


class DataContractError(DataError):
    pass


class QueryLimitError(DataError):
    pass


class ToolArgumentError(DataError):
    def __init__(self, parameter, constraint, recovery="Correct the parameter and retry the same selection"):
        self.parameter, self.constraint, self.recovery = parameter, constraint, recovery
        super().__init__(f"Invalid {parameter}: {constraint}")


def tool_error(exc, max_bytes=512):
    # Fixed keys and bounded values keep errors safe even for hostile arguments.
    result = {"error": {"code": type(exc).__name__,
                      "parameter": str(getattr(exc, "parameter", "selection"))[:60],
                      "constraint": redact_message(str(getattr(exc, "constraint", str(exc))))[:160],
                      "recovery": str(getattr(exc, "recovery", "Narrow the selection or correct the input and retry"))[:100]}}
    import json
    for key in ("constraint","recovery","parameter"):
        while len(json.dumps(result,ensure_ascii=False,separators=(",",":")).encode())>max_bytes and len(result["error"][key])>4:
            result["error"][key]=result["error"][key][:-4]
    return result

