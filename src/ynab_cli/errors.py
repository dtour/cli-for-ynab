from dataclasses import dataclass


@dataclass
class CliError(Exception):
    code: str
    message: str
    exit_code: int = 1
    status: int | None = None
    details: dict | None = None

    def __str__(self) -> str:
        return self.message

    def payload(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.status is not None:
            result["http_status"] = self.status
        if self.details is not None:
            result["details"] = self.details
        return {"error": result}


def reason_sentence(cause: CliError) -> str:
    """The cause's sanitised message as a sentence, for embedding in a wrapper error."""
    text = cause.message.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."
