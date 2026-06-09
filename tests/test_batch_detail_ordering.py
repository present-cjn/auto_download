from __future__ import annotations

from app.main import sort_rows_by_excel_row


def test_sort_rows_by_excel_row() -> None:
    rows = [
        {"item": {"row_number": 5}},
        {"item": {"row_number": 2}},
        {"item": {"row_number": 9}},
    ]

    sorted_rows = sort_rows_by_excel_row(rows)

    assert [row["item"]["row_number"] for row in sorted_rows] == [2, 5, 9]
