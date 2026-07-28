import base64
import mimetypes
from pathlib import Path

_DEFAULT_MIME = "image/png"


def file_to_data_uri(path: str | Path) -> str:
    p = Path(path)
    data = p.read_bytes()
    mime = mimetypes.guess_type(p.name)[0] or _DEFAULT_MIME
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"
