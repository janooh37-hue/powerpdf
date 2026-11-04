"""The engine's card-child budget applies across independent desktop workspaces."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import numpy as np

from powerpdf_core import ocr


def test_two_workspaces_share_four_ocr_slots(monkeypatch):
    page = np.full((60, 90, 3), 220, dtype=np.uint8)
    rect = ((45, 30), (50, 25), 0)
    cards = tuple(ocr.OCRCard(str(index), 0, (page, page), (rect, rect)) for index in range(8))
    active = peak = 0
    lock = threading.Lock()
    start = threading.Barrier(2)

    def recognize(sides, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            return '16-16401', 0
        finally:
            with lock:
                active -= 1

    def workspace():
        start.wait(timeout=5)
        return tuple(ocr.read_names(cards, executable='local-test-tesseract'))

    monkeypatch.setattr(ocr, 'suggest_name', recognize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in (pool.submit(workspace), pool.submit(workspace))]
    assert len(results[0]) == len(results[1]) == 8
    assert 1 <= peak <= 4
    assert active == 0
    assert all(result.name == '16-16401' for batch in results for result in batch)


def test_real_tesseract_only_proposes_a_name(tmp_path):
    import pytest
    from gui_test_fixtures import multi_card_scan
    from powerpdf_core.cards import load_cards

    executable = ocr.resolve_tesseract()
    if not executable:
        pytest.skip('Local Tesseract is not installed; bundled binary is qualified by packaging')
    source = multi_card_scan(tmp_path / 'رخصة المركبة.pdf')
    session, _ = load_cards([source])
    result, = ocr.read_names(session.ocr_cards()[:1], executable=executable)
    assert result.name == '16-16401'
    assert session.apply_suggestion(result)
    assert session.suggested[0] == ('16-16401', 0)
    assert session.names[0] is None
    session.name(0, '16-16401 رخصة')
    assert session.names[0] == '16-16401 رخصة'
