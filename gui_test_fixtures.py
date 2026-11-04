"""Generated local scan fixtures: real vector PDF cards, not owner documents."""
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def multi_card_scan(path: Path, *, missing=False) -> Path:
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')}))
    for page_number in range(2):
        page = writer.add_blank_page(595, 842)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        commands = []
        for row in range(4):
            number = page_number * 4 + row + 1
            for side, x in enumerate((30, 323)):
                if missing and page_number == 1 and row == 3 and side == 0:
                    continue
                y, w, h = 666 - row * 198, 242, 153
                color = '.78 .85 .72' if side else '.76 .80 .87'
                commands += [f'q {color} rg {x} {y} {w} {h} re f 0.15 0.2 0.2 RG .7 w {x} {y} {w} {h} re S',
                    f'0.12 0.18 0.16 rg BT /F1 12 Tf {x+12} {y+128} Td (VEHICLE LICENSE - {"FRONT" if side else "BACK"}) Tj ET',
                    f'BT /F1 9 Tf {x+12} {y+108} Td (SYNTHETIC LOCAL SCAN / CARD {number:02d}) Tj ET',
                    f'BT /F1 11 Tf {x+12} {y+78} Td (Traffic Plate No. 16 / {16400+number}) Tj ET',
                    f'BT /F1 8 Tf {x+12} {y+56} Td (Vehicle: Sample Sedan 2026) Tj ET',
                    f'BT /F1 8 Tf {x+12} {y+40} Td (Registration: 2026   Expiry: 2027) Tj ET',
                    f'BT /F1 7 Tf {x+12} {y+18} Td (No personal information - PowerPDF test fixture) Tj ET Q']
        stream = DecodedStreamObject()
        stream.set_data('\n'.join(commands).encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_metadata({'/Title': 'Synthetic two-page four-row license batch'})
    writer.write(path)
    writer.close()
    return path
