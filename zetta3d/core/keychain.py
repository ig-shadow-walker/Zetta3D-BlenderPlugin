"""The user's own AI keys, one per provider, in the operating system's credential store: the macOS Keychain,
the Windows Credential Manager, or the Secret Service on Linux.

NEVER THE PREFERENCES. Blender's userpref.blend is a plain file; a paid API key does not belong there. The
credential store encrypts at rest and is per-user. A key is read to check it or to send a message and nowhere
else; the add-on never shows one again after it is saved, and never writes one to a log.
"""

import ctypes
import os
import shutil
import subprocess
import sys

SERVICE = "Zetta 3D for Blender"

_known = {}  # provider → has a key, so panels don't hit the store on every draw
_listeners = []


def on_change(listener):
    _listeners.append(listener)


def _changed():
    for listener in list(_listeners):
        try:
            listener()
        except Exception as error:  # noqa: BLE001
            print(f"[Zetta 3D] {error!r}")


def has(provider):
    if provider not in _known:
        _known[provider] = bool(get(provider))
    return _known[provider]


def get(provider):
    """The saved key, or None."""
    try:
        return _store().read(provider)
    except Exception as error:  # noqa: BLE001
        print(f"[Zetta 3D] Could not read the saved key from the system key store ({type(error).__name__}).")
        return None


def set(provider, key):  # noqa: A001 — mirrors get/has
    """Saves or replaces the key. Returns an error to show, or None."""
    key = (key or "").strip()
    if not key:
        return "Paste a key first."
    try:
        _store().write(provider, key)
    except Exception as error:  # noqa: BLE001
        return f"Could not save the key in {store_name()}: {error}"
    _known[provider] = True
    _changed()
    return None


def delete(provider):
    try:
        _store().remove(provider)
    except Exception as error:  # noqa: BLE001
        print(f"[Zetta 3D] Could not remove the saved key ({type(error).__name__}).")
    _known[provider] = False
    _changed()


def store_name():
    if sys.platform == "darwin":
        return "your macOS Keychain"
    if sys.platform == "win32":
        return "Windows Credential Manager"
    return "your system keyring" if _LinuxStore.has_secret_tool() else "a private file in your home folder"


def _store():
    if sys.platform == "darwin":
        return _MacStore
    if sys.platform == "win32":
        return _WindowsStore
    return _LinuxStore


# ─── macOS: Keychain Services ─────────────────────────────────────────────────────────────────────


class _MacStore:
    """The login keychain, through Security.framework directly. Not the `security` tool: that takes the
    password on its command line, where any process listing would show it."""

    NOT_FOUND = -25300  # errSecItemNotFound
    _lib = None

    @classmethod
    def lib(cls):
        if cls._lib is None:
            sec = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            vp, u32, i32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
            sec.SecKeychainFindGenericPassword.argtypes = [
                vp, u32, ctypes.c_char_p, u32, ctypes.c_char_p,
                ctypes.POINTER(u32), ctypes.POINTER(vp), ctypes.POINTER(vp),
            ]
            sec.SecKeychainFindGenericPassword.restype = i32
            sec.SecKeychainAddGenericPassword.argtypes = [
                vp, u32, ctypes.c_char_p, u32, ctypes.c_char_p, u32, ctypes.c_char_p, ctypes.POINTER(vp),
            ]
            sec.SecKeychainAddGenericPassword.restype = i32
            sec.SecKeychainItemModifyAttributesAndData.argtypes = [vp, vp, u32, ctypes.c_char_p]
            sec.SecKeychainItemModifyAttributesAndData.restype = i32
            sec.SecKeychainItemDelete.argtypes = [vp]
            sec.SecKeychainItemDelete.restype = i32
            sec.SecKeychainItemFreeContent.argtypes = [vp, vp]
            sec.SecKeychainItemFreeContent.restype = i32
            cf.CFRelease.argtypes = [vp]
            cf.CFRelease.restype = None
            cls._lib = (sec, cf)
        return cls._lib

    @classmethod
    def _find(cls, provider, want_secret=True):
        sec, _ = cls.lib()
        service = SERVICE.encode("utf-8")
        account = provider.encode("utf-8")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = sec.SecKeychainFindGenericPassword(
            None, len(service), service, len(account), account,
            ctypes.byref(length), ctypes.byref(data) if want_secret else None, ctypes.byref(item),
        )
        if status != 0:
            return status, None, None
        secret = None
        if want_secret and data.value:
            secret = ctypes.string_at(data.value, length.value)
            sec.SecKeychainItemFreeContent(None, data)
        return 0, secret, item

    @staticmethod
    def _check(status):
        if status != 0:
            raise OSError(f"Keychain error {status}.")

    @classmethod
    def read(cls, provider):
        status, secret, item = cls._find(provider)
        if status == cls.NOT_FOUND:
            return None
        cls._check(status)
        cls.lib()[1].CFRelease(item)
        return secret.decode("utf-8") if secret else None

    @classmethod
    def write(cls, provider, key):
        sec, cf = cls.lib()
        data = key.encode("utf-8")
        status, _, item = cls._find(provider, want_secret=False)
        if status == 0:
            try:
                cls._check(sec.SecKeychainItemModifyAttributesAndData(item, None, len(data), data))
            finally:
                cf.CFRelease(item)
            return
        if status != cls.NOT_FOUND:
            cls._check(status)
        service = SERVICE.encode("utf-8")
        account = provider.encode("utf-8")
        added = ctypes.c_void_p()
        cls._check(sec.SecKeychainAddGenericPassword(
            None, len(service), service, len(account), account, len(data), data, ctypes.byref(added)))
        if added.value:
            cf.CFRelease(added)

    @classmethod
    def remove(cls, provider):
        sec, cf = cls.lib()
        status, _, item = cls._find(provider, want_secret=False)
        if status == cls.NOT_FOUND:
            return
        cls._check(status)
        try:
            cls._check(sec.SecKeychainItemDelete(item))
        finally:
            cf.CFRelease(item)


# ─── Windows: Credential Manager ──────────────────────────────────────────────────────────────────


class _WindowsStore:
    GENERIC = 1  # CRED_TYPE_GENERIC
    LOCAL_MACHINE = 2  # CRED_PERSIST_LOCAL_MACHINE: this user, this machine
    NOT_FOUND = 1168  # ERROR_NOT_FOUND
    _api = None

    @classmethod
    def api(cls):
        if cls._api is None:
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

            class CREDENTIAL(ctypes.Structure):
                _fields_ = [
                    ("Flags", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR),
                    ("LastWritten", FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR),
                ]

            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
            advapi.CredWriteW.restype = wintypes.BOOL
            advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                         ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
            advapi.CredReadW.restype = wintypes.BOOL
            advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            advapi.CredDeleteW.restype = wintypes.BOOL
            advapi.CredFree.argtypes = [ctypes.c_void_p]
            advapi.CredFree.restype = None
            cls._api = (advapi, CREDENTIAL)
        return cls._api

    @staticmethod
    def _target(provider):
        return f"{SERVICE}/{provider}"

    @classmethod
    def read(cls, provider):
        advapi, credential_type = cls.api()
        pointer = ctypes.POINTER(credential_type)()
        if not advapi.CredReadW(cls._target(provider), cls.GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == cls.NOT_FOUND:
                return None
            raise OSError(f"Credential Manager error {error}.")
        try:
            cred = pointer.contents
            return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-8")
        finally:
            advapi.CredFree(pointer)

    @classmethod
    def write(cls, provider, key):
        advapi, credential_type = cls.api()
        data = key.encode("utf-8")
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        cred = credential_type()
        cred.Type = cls.GENERIC
        cred.TargetName = cls._target(provider)
        cred.CredentialBlobSize = len(data)
        cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = cls.LOCAL_MACHINE
        cred.UserName = provider
        if not advapi.CredWriteW(ctypes.byref(cred), 0):
            raise OSError(f"Credential Manager error {ctypes.get_last_error()}.")

    @classmethod
    def remove(cls, provider):
        advapi, _ = cls.api()
        if not advapi.CredDeleteW(cls._target(provider), cls.GENERIC, 0):
            error = ctypes.get_last_error()
            if error != cls.NOT_FOUND:
                raise OSError(f"Credential Manager error {error}.")


# ─── Linux: Secret Service, or a private file ─────────────────────────────────────────────────────


class _LinuxStore:
    """`secret-tool` (libsecret) when it is installed — it reads the secret from standard input, so the key
    never appears on a command line. Without it, a file only this user can read."""

    @staticmethod
    def has_secret_tool():
        return shutil.which("secret-tool") is not None

    @staticmethod
    def _file():
        return os.path.join(os.path.expanduser("~"), ".config", "zetta3d", "blender-provider-keys")

    @staticmethod
    def _run(args, secret=None):
        result = subprocess.run(
            ["secret-tool", *args], input=(secret or "").encode("utf-8"),
            capture_output=True, timeout=10, check=False,
        )
        return result.returncode, result.stdout.decode("utf-8", "replace")

    @classmethod
    def read(cls, provider):
        if cls.has_secret_tool():
            code, output = cls._run(["lookup", "service", SERVICE, "provider", provider])
            return output.rstrip("\n") if code == 0 and output else None
        return cls._read_file().get(provider)

    @classmethod
    def write(cls, provider, key):
        if cls.has_secret_tool():
            code, _ = cls._run(["store", "--label", f"{SERVICE} ({provider})", "service", SERVICE,
                                "provider", provider], key)
            if code != 0:
                raise OSError("secret-tool could not save the key.")
            return
        keys = cls._read_file()
        keys[provider] = key
        cls._write_file(keys)

    @classmethod
    def remove(cls, provider):
        if cls.has_secret_tool():
            cls._run(["clear", "service", SERVICE, "provider", provider])
            return
        keys = cls._read_file()
        if keys.pop(provider, None) is not None:
            cls._write_file(keys)

    @classmethod
    def _read_file(cls):
        keys = {}
        try:
            with open(cls._file(), "r", encoding="utf-8") as handle:
                for line in handle:
                    name, sep, value = line.rstrip("\n").partition("=")
                    if sep and name:
                        keys[name] = value
        except OSError:
            pass
        return keys

    @classmethod
    def _write_file(cls, keys):
        file = cls._file()
        os.makedirs(os.path.dirname(file), exist_ok=True)
        # Owner read/write only from the moment it exists: the same promise a keyring makes, as far as a file can.
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.writelines(f"{name}={value}\n" for name, value in keys.items())
        os.chmod(file, 0o600)
