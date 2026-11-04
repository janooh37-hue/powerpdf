"""Use headless Qt by default for automated tests, never in the application."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp_cls():
    from powerpdf_gui.application import PowerPDFApplication
    return PowerPDFApplication
