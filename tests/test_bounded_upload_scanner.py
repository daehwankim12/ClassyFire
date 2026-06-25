from __future__ import annotations

from io import BytesIO

from apps.batches.upload import iter_text_records


class CountingReader(BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.max_read_size = 0

    def read(self, size: int = -1) -> bytes:
        self.max_read_size = max(self.max_read_size, size)
        return super().read(size)


def test_no_newline_large_payload_is_bounded_and_rejected(settings):
    payload = b"C" * (settings.MAX_STRUCTURE_LEN + 50_000)
    reader = CountingReader(payload)
    rows = list(iter_text_records(reader, filename="input.txt"))
    assert reader.max_read_size <= 65536
    assert len(rows) == 1
    assert rows[0]["parse_error"] == "ROW_TOO_LONG"
    assert rows[0]["overflow_length"] == len(payload)
