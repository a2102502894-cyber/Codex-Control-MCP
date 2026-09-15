from __future__ import annotations
import ctypes, os, secrets
from .errors import BridgeError


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise BridgeError(
            "authentication_required",
            "Set the token environment variable on non-Windows; plaintext token files are not generated.",
        )
    buf = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = DATA_BLOB()
    fn = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    ok = fn(ctypes.byref(blob), None, None, None, None, 1, ctypes.byref(out))
    if not ok:
        raise BridgeError(
            "authentication_required", "Windows DPAPI could not access the owner token."
        )
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def owner_token(cfg, create=False):
    token = os.environ.get(cfg.http.get("token_env", "CODEX_CONTROL_MCP_TOKEN"), "")
    if token:
        from .http_validation import bearer_credential

        try:
            valid = (
                32 <= len(token) <= 512
                and bearer_credential(b"Bearer " + token.encode("ascii")) is not None
            )
        except UnicodeError:
            valid = False
        if not valid:
            raise BridgeError(
                "invalid_config",
                "HTTP owner token must contain 32 to 512 valid ASCII Bearer token characters.",
            )
        return token
    path = cfg.home / "state/http-token.dpapi"
    if path.is_file():
        return _crypt(path.read_bytes(), True).decode("ascii")
    if not create:
        raise BridgeError(
            "authentication_required",
            "Run install or set the configured token environment variable.",
        )
    token = secrets.token_urlsafe(48)
    payload = _crypt(token.encode("ascii"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as f:
            f.write(payload)
    except FileExistsError:
        return _crypt(path.read_bytes(), True).decode("ascii")
    return token
