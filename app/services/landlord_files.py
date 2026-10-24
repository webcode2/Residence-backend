"""Bounded CSV / Excel parsing; validate the whole file before queueing anything."""
import csv
import io
import zipfile
from pathlib import Path
from fastapi import HTTPException
from pydantic import ValidationError
from app.schemas.landlord import LandlordImportSchema

MAX_ROWS = 500
MAX_BYTES = 5 * 1024 * 1024
ALIASES = {'name':'name','landlord':'name','landlord_name':'name','full_name':'name',
    'email':'email','email_address':'email','phone':'phone','phone_number':'phone',
    'house_number':'house_number','unit':'house_number','house':'house_number',
    'street':'street_name','street_name':'street_name'}

def parse_file(filename, content):
    if len(content) > MAX_BYTES:
        raise HTTPException(413, 'File exceeds 5 MB')
    suffix = Path(filename or '').suffix.lower()
    try:
        if suffix == '.csv':
            rows = csv.reader(io.StringIO(content.decode('utf-8-sig')))
        elif suffix == '.xlsx':
            from openpyxl import load_workbook
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 20 * 1024 * 1024:
                    raise ValueError('Workbook is too large when expanded')
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            rows = workbook.active.iter_rows(values_only=True)
        elif suffix == '.xls':
            import xlrd
            workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
            sheet = workbook.sheet_by_index(0)
            rows = (sheet.row_values(i) for i in range(sheet.nrows))
        else:
            raise ValueError('Choose a CSV, XLSX or XLS file')
        headers = next(rows)
        keys = [ALIASES.get(str(h or '').strip().lower().replace(' ', '_')) for h in headers]
        if not {'name','email','phone','house_number'}.issubset(keys):
            raise ValueError('Required columns: Name, Email, Phone, House Number')
        mapped = [key for key in keys if key]
        if len(mapped) != len(set(mapped)):
            raise ValueError('Duplicate column headings are not allowed')
        results = []
        for number, row in enumerate(rows, start=2):
            if number > MAX_ROWS + 1:
                raise ValueError('Import up to 500 rows per file')
            if not any(value not in (None,'') for value in row):
                continue
            values = {key: str(int(value) if isinstance(value, float) and value.is_integer() else value).strip()
                for key, value in zip(keys,row) if key and value is not None}
            try:
                results.append(LandlordImportSchema(**values))
            except ValidationError as exc:
                fields = ', '.join(str(e['loc'][0]) for e in exc.errors())
                raise ValueError(f'Row {number}: check {fields}') from None
        if not results:
            raise ValueError('The file has no landlord rows')
        return results
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, StopIteration)) else 'Unable to read this spreadsheet'
        raise HTTPException(422, message or 'The file is empty') from None
    finally:
        if 'workbook' in locals():
            if hasattr(workbook, 'close'):
                workbook.close()
            elif hasattr(workbook, 'release_resources'):
                workbook.release_resources()
