"""The graphical interface (needs the ``gui`` extra: PySide6, pypdfium2)."""


def main(argv: list[str] | None = None) -> int:
    """``slim-pdf-gui``: says what to install instead of a traceback when
    slim-pdf was installed without the GUI's packages."""
    try:
        from .app import main as app_main
    except ImportError as e:
        import sys

        from .. import i18n
        from ..i18n import _

        i18n.setup()
        print(_("error: {message}").format(message=_(
            "the GUI needs PySide6: pip install 'slim-pdf[gui]' ({error})").format(error=e)),
            file=sys.stderr)
        return 1
    return app_main(argv)
