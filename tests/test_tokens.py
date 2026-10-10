"""Finding PKCS#11 modules: p11-kit's registry, the usual places, the proxy."""

import os
import sys

import pytest

from pdfsigner import tokens


@pytest.fixture
def system(tmp_path, monkeypatch):
    """An empty fake system: library, p11-kit and user config directories."""
    for d in ("lib/pkcs11", "share/modules", "etc/modules", "home/pkcs11/modules"):
        (tmp_path / d).mkdir(parents=True)
    monkeypatch.setattr(tokens, "KNOWN_MODULES", {"linux": [
        str(tmp_path / "lib" / "opensc-*.so"), str(tmp_path / "lib" / "pkcs11" / "*.so")]})
    monkeypatch.setattr(tokens, "P11_KIT_CONFIG",
                        [str(tmp_path / "share" / "modules"), str(tmp_path / "etc" / "modules")])
    monkeypatch.setattr(tokens, "P11_KIT_MODULE_DIRS", [str(tmp_path / "lib" / "pkcs11")])
    monkeypatch.setattr(tokens, "P11_KIT_PROXY", [str(tmp_path / "lib" / "p11-kit-proxy.so")])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home"))
    return tmp_path


def lib(system, name: str) -> str:
    path = system / "lib" / name
    path.write_bytes(b"")
    return str(path)


def register(system, where: str, name: str, text: str) -> None:
    d = {"share": "share/modules", "etc": "etc/modules", "user": "home/pkcs11/modules"}[where]
    (system / d / f"{name}.module").write_text(text, encoding="utf-8")


def test_usual_places_and_proxy_fallback(system):
    proxy = lib(system, "p11-kit-proxy.so")
    assert tokens.find_modules("linux") == [proxy]  # nothing found directly
    opensc = lib(system, "opensc-pkcs11.so")
    assert tokens.find_modules("linux") == [opensc]
    assert tokens.find_modules("win32") == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_the_same_module_only_once(system):
    opensc = lib(system, "opensc-pkcs11.so")
    os.symlink(opensc, system / "lib" / "pkcs11" / "opensc-pkcs11.so")
    register(system, "share", "opensc", f"module: {opensc}\n")
    assert tokens.find_modules("linux") == [opensc]


def test_p11_kit_registry(system):
    vendor = system / "opt" / "vendor.so"
    vendor.parent.mkdir()
    vendor.write_bytes(b"")
    ykcs11 = lib(system, "pkcs11/libykcs11.so")
    lib(system, "pkcs11/p11-kit-trust.so")
    lib(system, "pkcs11/gnome-keyring-pkcs11.so")
    register(system, "share", "vendor", f"# vendor's card\nmodule: {vendor}\npriority: 5\n")
    register(system, "share", "ykcs11", "module: libykcs11.so\n")  # in the module directory
    register(system, "share", "trust", "module: p11-kit-trust.so\ntrust-policy: yes\n")
    register(system, "share", "missing", "module: /no/such/module.so\n")
    register(system, "share", "empty", "# no module line\n")
    assert tokens.find_modules("linux") == [str(vendor), ykcs11]  # trust & keyring skipped


def test_p11_kit_enable_and_disable(system):
    a, b, c = (lib(system, f"{n}.so") for n in "abc")
    register(system, "share", "a", f"module: {a}\ndisable-in: firefox pdfsigner\n")
    register(system, "share", "b", f"module: {b}\nenable-in: virt-viewer\n")
    register(system, "share", "c", f"module: {c}\nenable-in: pdfsigner firefox\n")
    assert tokens.find_modules("linux") == [c]


def test_later_registry_directories_win(system):
    a, b = lib(system, "a.so"), lib(system, "b.so")
    register(system, "share", "card", f"module: {a}\n")
    register(system, "etc", "card", f"module: {b}\n")
    assert tokens.find_modules("linux") == [b]
    register(system, "user", "card", f"module: {b}\ndisable-in: pdfsigner\n")
    assert tokens.find_modules("linux") == []
