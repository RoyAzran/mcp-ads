"""
Google Sheets tools - adapted for unified remote MCP server.
"""

import os
import json
from typing import List, Dict, Any, Optional, Union

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from mcp_instance import mcp
from credentials import connect_hint
from auth import current_user_ctx
from permissions import require_editor

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]


def _google_creds():
    """Build per-user Google OAuth credentials from the current request context."""
    user = current_user_ctx.get(None)
    if user is None:
        raise RuntimeError("Not authenticated.")
    from credentials import provider
    rt = provider().google_token("sheets")
    if not rt:
        raise RuntimeError("Google Sheets account not connected. Connect your Google account via " + connect_hint() + ".")
    creds = Credentials(
        token=None,
        refresh_token=rt,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def _sheets_service():
    return build('sheets', 'v4', credentials=_google_creds())


def _drive_service():
    return build('drive', 'v3', credentials=_google_creds())


def _col_letter(n: int) -> str:
    """Convert 0-based column index to letter (0→A, 25→Z, 26→AA)."""
    result = ""
    while True:
        result = chr(n % 26 + ord('A')) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


def _col_index(letter: str) -> int:
    """Convert column letter to 0-based index."""
    letter = letter.upper()
    result = 0
    for ch in letter:
        result = result * 26 + (ord(ch) - ord('A') + 1)
    return result - 1


def _get_sheet_id(sheets_svc, spreadsheet_id: str, sheet_name: str) -> Optional[int]:
    meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get('sheets', []):
        if s['properties']['title'] == sheet_name:
            return s['properties']['sheetId']
    return None


def _color(hex_color: str) -> Dict:
    """Convert #RRGGBB to Google Sheets color object."""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join(c * 2 for c in hex_color)
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return {"red": r, "green": g, "blue": b}


def _range_to_grid(spreadsheet_id: str, sheet_id: int, range_a1: Optional[str] = None) -> Dict:
    """Build a GridRange from A1 notation or full sheet if None."""
    gr = {"sheetId": sheet_id}
    if range_a1:
        # e.g. A1:C5 or A:C or 1:5
        import re
        m = re.match(r'^([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?$', range_a1.upper())
        if m:
            sc, sr, ec, er = m.groups()
            if sc:
                gr["startColumnIndex"] = _col_index(sc)
            if sr:
                gr["startRowIndex"] = int(sr) - 1
            if ec:
                gr["endColumnIndex"] = _col_index(ec) + 1
            if er:
                gr["endRowIndex"] = int(er)
    return gr


# ─────────────────────────────────────────────
# 1. SPREADSHEET MANAGEMENT
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_create_spreadsheet(title: str, sheet_names: Optional[List[str]] = None) -> Dict:
    """
    Create a new Google Spreadsheet.

    Args:
        title: Name of the spreadsheet.
        sheet_names: Optional list of initial sheet names (default: ['Sheet1']).

    Returns:
        Dict with spreadsheetId and URL.
    """
    if (err := require_editor("sheets_create_spreadsheet")): return err
    sheets = _sheets_service()
    body = {"properties": {"title": title}}
    if sheet_names:
        body["sheets"] = [{"properties": {"title": n}} for n in sheet_names]
    result = sheets.spreadsheets().create(body=body).execute()
    sid = result['spreadsheetId']
    return {
        "spreadsheetId": sid,
        "url": f"https://docs.google.com/spreadsheets/d/{sid}",
        "title": title
    }


@mcp.tool()
def sheets_delete_spreadsheet(spreadsheet_id: str) -> Dict:
    """
    Delete a spreadsheet permanently from Google Drive.

    Args:
        spreadsheet_id: The spreadsheet ID.
    """
    if (err := require_editor("sheets_delete_spreadsheet")): return err
    drive = _drive_service()
    drive.files().delete(fileId=spreadsheet_id).execute()
    return {"success": True, "deleted": spreadsheet_id}


@mcp.tool()
def sheets_copy_spreadsheet(spreadsheet_id: str, new_title: str) -> Dict:
    """
    Copy an entire spreadsheet to a new file.

    Args:
        spreadsheet_id: Source spreadsheet ID.
        new_title: Name for the new spreadsheet.
    """
    if (err := require_editor("sheets_copy_spreadsheet")): return err
    drive = _drive_service()
    result = drive.files().copy(fileId=spreadsheet_id, body={"name": new_title}).execute()
    new_id = result['id']
    return {
        "spreadsheetId": new_id,
        "url": f"https://docs.google.com/spreadsheets/d/{new_id}",
        "title": new_title
    }


@mcp.tool()
def sheets_get_spreadsheet_info(spreadsheet_id: str) -> Dict:
    """
    Get full metadata about a spreadsheet (sheets, properties, named ranges, etc.).

    Args:
        spreadsheet_id: The spreadsheet ID.
    """
    sheets = _sheets_service()
    result = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    info = {
        "spreadsheetId": result['spreadsheetId'],
        "title": result['properties']['title'],
        "locale": result['properties'].get('locale'),
        "timeZone": result['properties'].get('timeZone'),
        "sheets": [],
        "namedRanges": result.get('namedRanges', [])
    }
    for s in result.get('sheets', []):
        p = s['properties']
        info["sheets"].append({
            "sheetId": p['sheetId'],
            "title": p['title'],
            "index": p['index'],
            "sheetType": p.get('sheetType', 'GRID'),
            "rowCount": p.get('gridProperties', {}).get('rowCount'),
            "columnCount": p.get('gridProperties', {}).get('columnCount'),
            "hidden": p.get('hidden', False),
            "tabColor": p.get('tabColorStyle', {})
        })
    return info


@mcp.tool()
def sheets_list_spreadsheets(folder_id: Optional[str] = None, search_query: Optional[str] = None) -> List[Dict]:
    """
    List Google Spreadsheets in Drive (optionally filtered by folder or search).

    Args:
        folder_id: Optional Drive folder ID to filter.
        search_query: Optional text to search in file names.
    """
    drive = _drive_service()
    q = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    if folder_id:
        q += f" and '{folder_id}' in parents"
    if search_query:
        q += f" and name contains '{search_query}'"
    result = drive.files().list(q=q, fields="files(id,name,modifiedTime,webViewLink)", pageSize=100).execute()
    return result.get('files', [])


@mcp.tool()
def sheets_share_spreadsheet(spreadsheet_id: str, email: str, role: str = "reader") -> Dict:
    """
    Share a spreadsheet with a user.

    Args:
        spreadsheet_id: The spreadsheet ID.
        email: Email address to share with.
        role: Permission role: 'reader', 'commenter', or 'writer'.
    """
    if (err := require_editor("sheets_share_spreadsheet")): return err
    drive = _drive_service()
    perm = {"type": "user", "role": role, "emailAddress": email}
    result = drive.permissions().create(
        fileId=spreadsheet_id,
        body=perm,
        sendNotificationEmail=False
    ).execute()
    return {"success": True, "permissionId": result.get('id'), "role": role, "email": email}


# ─────────────────────────────────────────────
# 2. SHEET (TAB) MANAGEMENT
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_list_sheets(spreadsheet_id: str) -> List[Dict]:
    """
    List all sheets/tabs in a spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
    """
    sheets = _sheets_service()
    result = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return [
        {
            "sheetId": s['properties']['sheetId'],
            "title": s['properties']['title'],
            "index": s['properties']['index'],
            "hidden": s['properties'].get('hidden', False)
        }
        for s in result.get('sheets', [])
    ]


@mcp.tool()
def sheets_create_sheet(spreadsheet_id: str, title: str, index: Optional[int] = None,
                 rows: int = 1000, columns: int = 26) -> Dict:
    """
    Add a new sheet/tab to an existing spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        title: Name for the new sheet.
        index: Position index (0-based). If None, appended at end.
        rows: Initial row count (default 1000).
        columns: Initial column count (default 26).
    """
    if (err := require_editor("sheets_create_sheet")): return err
    sheets = _sheets_service()
    props = {"title": title, "gridProperties": {"rowCount": rows, "columnCount": columns}}
    if index is not None:
        props["index"] = index
    req = {"addSheet": {"properties": props}}
    result = sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": [req]}).execute()
    new_props = result['replies'][0]['addSheet']['properties']
    return {"sheetId": new_props['sheetId'], "title": new_props['title'], "index": new_props['index']}


@mcp.tool()
def sheets_delete_sheet(spreadsheet_id: str, sheet_name: str) -> Dict:
    """
    Delete a sheet/tab from a spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Name of the sheet to delete.
    """
    if (err := require_editor("sheets_delete_sheet")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"deleteSheet": {"sheetId": sheet_id}}]}
    ).execute()
    return {"success": True, "deleted": sheet_name}


@mcp.tool()
def sheets_rename_sheet(spreadsheet_id: str, old_name: str, new_name: str) -> Dict:
    """
    Rename a sheet/tab.

    Args:
        spreadsheet_id: The spreadsheet ID.
        old_name: Current sheet name.
        new_name: New sheet name.
    """
    if (err := require_editor("sheets_rename_sheet")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, old_name)
    if sheet_id is None:
        return {"error": f"Sheet '{old_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "title": new_name},
            "fields": "title"
        }}]}
    ).execute()
    return {"success": True, "renamed": {"from": old_name, "to": new_name}}


@mcp.tool()
def sheets_copy_sheet(spreadsheet_id: str, sheet_name: str, destination_spreadsheet_id: Optional[str] = None) -> Dict:
    """
    Copy a sheet to the same or a different spreadsheet.

    Args:
        spreadsheet_id: Source spreadsheet ID.
        sheet_name: Name of the sheet to copy.
        destination_spreadsheet_id: Target spreadsheet ID (defaults to same spreadsheet).
    """
    if (err := require_editor("sheets_copy_sheet")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    dest_id = destination_spreadsheet_id or spreadsheet_id
    result = sheets.spreadsheets().sheets().copyTo(
        spreadsheetId=spreadsheet_id,
        sheetId=sheet_id,
        body={"destinationSpreadsheetId": dest_id}
    ).execute()
    return {"newSheetId": result['sheetId'], "newTitle": result['title']}


@mcp.tool()
def sheets_move_sheet(spreadsheet_id: str, sheet_name: str, new_index: int) -> Dict:
    """
    Reorder/move a sheet to a different position.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Name of the sheet.
        new_index: New 0-based index position.
    """
    if (err := require_editor("sheets_move_sheet")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "index": new_index},
            "fields": "index"
        }}]}
    ).execute()
    return {"success": True, "sheet": sheet_name, "newIndex": new_index}


@mcp.tool()
def sheets_set_sheet_tab_color(spreadsheet_id: str, sheet_name: str, color_hex: str) -> Dict:
    """
    Set the color of a sheet tab.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Name of the sheet.
        color_hex: Color in #RRGGBB format (e.g. '#FF0000' for red).
    """
    if (err := require_editor("sheets_set_sheet_tab_color")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "tabColorStyle": {"rgbColor": _color(color_hex)}
            },
            "fields": "tabColorStyle"
        }}]}
    ).execute()
    return {"success": True, "sheet": sheet_name, "tabColor": color_hex}


@mcp.tool()
def sheets_hide_sheet(spreadsheet_id: str, sheet_name: str) -> Dict:
    """Hide a sheet tab (it remains in the spreadsheet but is not visible)."""
    if (err := require_editor("sheets_hide_sheet")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "hidden": True},
            "fields": "hidden"
        }}]}
    ).execute()
    return {"success": True, "hidden": sheet_name}


@mcp.tool()
def sheets_show_sheet(spreadsheet_id: str, sheet_name: str) -> Dict:
    """Show/unhide a previously hidden sheet tab."""
    if (err := require_editor("sheets_show_sheet")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "hidden": False},
            "fields": "hidden"
        }}]}
    ).execute()
    return {"success": True, "shown": sheet_name}


# ─────────────────────────────────────────────
# 3. READING DATA
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_get_sheet_data(spreadsheet_id: str, sheet: str, range: Optional[str] = None) -> Dict:
    """
    Read values from a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet: Sheet name.
        range: Optional A1 range (e.g. 'A1:D10'). If omitted, reads entire sheet.

    Returns:
        Dict with 'values' (2D list) and range info.
    """
    sheets = _sheets_service()
    full_range = f"{sheet}!{range}" if range else sheet
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=full_range
    ).execute()
    return {
        "spreadsheetId": spreadsheet_id,
        "range": result.get('range'),
        "values": result.get('values', [])
    }


@mcp.tool()
def sheets_get_sheet_formulas(spreadsheet_id: str, sheet: str, range: Optional[str] = None) -> Dict:
    """
    Read raw formulas (not computed values) from a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet: Sheet name.
        range: Optional A1 range.
    """
    sheets = _sheets_service()
    full_range = f"{sheet}!{range}" if range else sheet
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=full_range,
        valueRenderOption='FORMULA'
    ).execute()
    return {
        "spreadsheetId": spreadsheet_id,
        "range": result.get('range'),
        "formulas": result.get('values', [])
    }


@mcp.tool()
def sheets_get_multiple_ranges(spreadsheet_id: str, ranges: List[str]) -> List[Dict]:
    """
    Read multiple ranges from a spreadsheet in one API call.

    Args:
        spreadsheet_id: The spreadsheet ID.
        ranges: List of ranges in 'SheetName!A1:B5' or 'SheetName' format.
    """
    sheets = _sheets_service()
    result = sheets.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=ranges
    ).execute()
    return [
        {"range": vr.get('range'), "values": vr.get('values', [])}
        for vr in result.get('valueRanges', [])
    ]


@mcp.tool()
def sheets_get_cell_formatting(spreadsheet_id: str, sheet: str, range: str) -> Dict:
    """
    Get formatting details for a cell range (font, colors, borders, number format, etc.).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet: Sheet name.
        range: A1 range (e.g. 'A1:C5').
    """
    sheets = _sheets_service()
    full_range = f"{sheet}!{range}"
    result = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        ranges=[full_range],
        includeGridData=True
    ).execute()
    return result


@mcp.tool()
def sheets_find_in_spreadsheet(spreadsheet_id: str, search_text: str,
                        sheet: Optional[str] = None,
                        match_case: bool = False,
                        match_entire_cell: bool = False) -> List[Dict]:
    """
    Search for text across all sheets or a specific sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        search_text: Text to find.
        sheet: Optional sheet name to restrict search.
        match_case: Case-sensitive search.
        match_entire_cell: Match whole cell content only.
    """
    sheets = _sheets_service()
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_names = [s['properties']['title'] for s in meta.get('sheets', [])]
    if sheet:
        sheet_names = [n for n in sheet_names if n == sheet]

    matches = []
    for sname in sheet_names:
        result = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=sname
        ).execute()
        for ri, row in enumerate(result.get('values', [])):
            for ci, cell in enumerate(row):
                cell_str = str(cell)
                needle = search_text if match_case else search_text.lower()
                haystack = cell_str if match_case else cell_str.lower()
                found = (haystack == needle) if match_entire_cell else (needle in haystack)
                if found:
                    matches.append({
                        "sheet": sname,
                        "row": ri + 1,
                        "column": _col_letter(ci),
                        "cell": f"{_col_letter(ci)}{ri + 1}",
                        "value": cell_str
                    })
    return matches


# ─────────────────────────────────────────────
# 4. WRITING DATA
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_update_cells(spreadsheet_id: str, sheet: str, range: str,
                 values: List[List[Any]],
                 value_input_option: str = "USER_ENTERED") -> Dict:
    """
    Write values to a cell range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet: Sheet name.
        range: A1 range (e.g. 'A1:C3').
        values: 2D list of values. Use formulas like '=SUM(A1:A5)' as strings.
        value_input_option: 'USER_ENTERED' (parses formulas/types) or 'RAW' (literal strings).
    """
    if (err := require_editor("sheets_update_cells")): return err
    sheets = _sheets_service()
    full_range = f"{sheet}!{range}"
    result = sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=full_range,
        valueInputOption=value_input_option,
        body={"values": values}
    ).execute()
    return {
        "updatedRange": result.get('updatedRange'),
        "updatedRows": result.get('updatedRows'),
        "updatedColumns": result.get('updatedColumns'),
        "updatedCells": result.get('updatedCells')
    }


@mcp.tool()
def sheets_update_multiple_ranges(spreadsheet_id: str,
                           updates: List[Dict[str, Any]],
                           value_input_option: str = "USER_ENTERED") -> Dict:
    """
    Update multiple ranges in one API call (batch).

    Args:
        spreadsheet_id: The spreadsheet ID.
        updates: List of dicts, each with 'range' (e.g. 'Sheet1!A1:B2') and 'values' (2D list).
        value_input_option: 'USER_ENTERED' or 'RAW'.

    Example updates:
        [
            {"range": "Sheet1!A1", "values": [["Hello"]]},
            {"range": "Sheet1!B2:C3", "values": [[1, 2], [3, 4]]}
        ]
    """
    if (err := require_editor("sheets_update_multiple_ranges")): return err
    sheets = _sheets_service()
    data = [{"range": u["range"], "values": u["values"]} for u in updates]
    result = sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": value_input_option, "data": data}
    ).execute()
    return {
        "totalUpdatedCells": result.get('totalUpdatedCells'),
        "totalUpdatedRows": result.get('totalUpdatedRows'),
        "responses": result.get('responses', [])
    }


@mcp.tool()
def sheets_append_rows(spreadsheet_id: str, sheet: str, values: List[List[Any]],
                value_input_option: str = "USER_ENTERED",
                insert_data_option: str = "INSERT_ROWS") -> Dict:
    """
    Append rows after the last row with data.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet: Sheet name.
        values: 2D list of rows to append.
        value_input_option: 'USER_ENTERED' or 'RAW'.
        insert_data_option: 'INSERT_ROWS' (adds new rows) or 'OVERWRITE' (overwrites existing).
    """
    if (err := require_editor("sheets_append_rows")): return err
    sheets = _sheets_service()
    result = sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=sheet,
        valueInputOption=value_input_option,
        insertDataOption=insert_data_option,
        body={"values": values}
    ).execute()
    updates = result.get('updates', {})
    return {
        "updatedRange": updates.get('updatedRange'),
        "updatedRows": updates.get('updatedRows'),
        "updatedCells": updates.get('updatedCells')
    }


@mcp.tool()
def sheets_clear_range(spreadsheet_id: str, sheet: str, range: Optional[str] = None) -> Dict:
    """
    Clear values from a range (keeps formatting).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet: Sheet name.
        range: A1 range. If omitted, clears entire sheet.
    """
    if (err := require_editor("sheets_clear_range")): return err
    sheets = _sheets_service()
    full_range = f"{sheet}!{range}" if range else sheet
    result = sheets.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=full_range,
        body={}
    ).execute()
    return {"clearedRange": result.get('clearedRange')}


@mcp.tool()
def sheets_copy_range(spreadsheet_id: str, source_sheet: str, source_range: str,
               dest_sheet: str, dest_range: str,
               paste_type: str = "PASTE_NORMAL") -> Dict:
    """
    Copy a range to another location (within same spreadsheet).

    Args:
        spreadsheet_id: The spreadsheet ID.
        source_sheet: Source sheet name.
        source_range: Source A1 range (e.g. 'A1:C5').
        dest_sheet: Destination sheet name.
        dest_range: Destination start cell or range (e.g. 'E1').
        paste_type: What to paste:
            'PASTE_NORMAL' - values+formatting,
            'PASTE_VALUES' - values only,
            'PASTE_FORMAT' - formatting only,
            'PASTE_FORMULA' - formulas only.
    """
    if (err := require_editor("sheets_copy_range")): return err
    sheets = _sheets_service()
    src_sheet_id = _get_sheet_id(sheets, spreadsheet_id, source_sheet)
    dst_sheet_id = _get_sheet_id(sheets, spreadsheet_id, dest_sheet)
    if src_sheet_id is None:
        return {"error": f"Source sheet '{source_sheet}' not found"}
    if dst_sheet_id is None:
        return {"error": f"Destination sheet '{dest_sheet}' not found"}

    src_gr = _range_to_grid(spreadsheet_id, src_sheet_id, source_range)
    dst_gr = _range_to_grid(spreadsheet_id, dst_sheet_id, dest_range)

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"copyPaste": {
            "source": src_gr,
            "destination": dst_gr,
            "pasteType": paste_type,
            "pasteOrientation": "NORMAL"
        }}]}
    ).execute()
    return {"success": True, "copied": f"{source_sheet}!{source_range}", "to": f"{dest_sheet}!{dest_range}"}


@mcp.tool()
def sheets_move_range(spreadsheet_id: str, source_sheet: str, source_range: str,
               dest_sheet: str, dest_start_row: int, dest_start_col: int) -> Dict:
    """
    Move a range to a new location (cut+paste).

    Args:
        spreadsheet_id: The spreadsheet ID.
        source_sheet: Source sheet name.
        source_range: Source A1 range.
        dest_sheet: Destination sheet name.
        dest_start_row: Destination start row (1-based).
        dest_start_col: Destination start column (1-based).
    """
    if (err := require_editor("sheets_move_range")): return err
    sheets = _sheets_service()
    src_id = _get_sheet_id(sheets, spreadsheet_id, source_sheet)
    dst_id = _get_sheet_id(sheets, spreadsheet_id, dest_sheet)
    if src_id is None:
        return {"error": f"Source sheet '{source_sheet}' not found"}
    if dst_id is None:
        return {"error": f"Destination sheet '{dest_sheet}' not found"}

    src_gr = _range_to_grid(spreadsheet_id, src_id, source_range)
    dest = {"sheetId": dst_id, "rowIndex": dest_start_row - 1, "columnIndex": dest_start_col - 1}

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"moveDimension": {
            "source": {"sheetId": src_id, "dimension": "ROWS",
                       "startIndex": src_gr.get("startRowIndex", 0),
                       "endIndex": src_gr.get("endRowIndex", 1)},
            "destinationIndex": dest_start_row - 1
        }}]}
    ).execute()
    return {"success": True}


# ─────────────────────────────────────────────
# 5. ROW & COLUMN OPERATIONS
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_insert_rows(spreadsheet_id: str, sheet_name: str,
                start_row: int, count: int = 1,
                inherit_from_before: bool = False) -> Dict:
    """
    Insert blank rows into a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_row: Row number before which to insert (1-based).
        count: Number of rows to insert.
        inherit_from_before: If True, new rows inherit formatting from above row.
    """
    if (err := require_editor("sheets_insert_rows")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"insertDimension": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_row - 1,
                "endIndex": start_row - 1 + count
            },
            "inheritFromBefore": inherit_from_before
        }}]}
    ).execute()
    return {"success": True, "inserted": f"{count} row(s) at row {start_row}"}


@mcp.tool()
def sheets_delete_rows(spreadsheet_id: str, sheet_name: str,
                start_row: int, end_row: int) -> Dict:
    """
    Delete rows from a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_row: First row to delete (1-based, inclusive).
        end_row: Last row to delete (1-based, inclusive).
    """
    if (err := require_editor("sheets_delete_rows")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"deleteDimension": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_row - 1,
                "endIndex": end_row
            }
        }}]}
    ).execute()
    return {"success": True, "deleted": f"rows {start_row} to {end_row}"}


@mcp.tool()
def sheets_insert_columns(spreadsheet_id: str, sheet_name: str,
                   start_column: str, count: int = 1,
                   inherit_from_before: bool = False) -> Dict:
    """
    Insert blank columns into a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_column: Column letter before which to insert (e.g. 'C').
        count: Number of columns to insert.
        inherit_from_before: Inherit formatting from left column.
    """
    if (err := require_editor("sheets_insert_columns")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    col_idx = _col_index(start_column)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"insertDimension": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_idx,
                "endIndex": col_idx + count
            },
            "inheritFromBefore": inherit_from_before
        }}]}
    ).execute()
    return {"success": True, "inserted": f"{count} column(s) at {start_column}"}


@mcp.tool()
def sheets_delete_columns(spreadsheet_id: str, sheet_name: str,
                   start_column: str, end_column: str) -> Dict:
    """
    Delete columns from a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_column: First column letter to delete (e.g. 'C').
        end_column: Last column letter to delete (e.g. 'E').
    """
    if (err := require_editor("sheets_delete_columns")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    start_idx = _col_index(start_column)
    end_idx = _col_index(end_column) + 1
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"deleteDimension": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_idx,
                "endIndex": end_idx
            }
        }}]}
    ).execute()
    return {"success": True, "deleted": f"columns {start_column} to {end_column}"}


@mcp.tool()
def sheets_set_column_width(spreadsheet_id: str, sheet_name: str,
                     start_column: str, end_column: Optional[str] = None,
                     width_pixels: int = 100) -> Dict:
    """
    Set the pixel width of one or more columns.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_column: First column letter (e.g. 'A').
        end_column: Last column letter (inclusive). Defaults to same as start.
        width_pixels: Width in pixels.
    """
    if (err := require_editor("sheets_set_column_width")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    start_idx = _col_index(start_column)
    end_idx = _col_index(end_column) + 1 if end_column else start_idx + 1
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_idx,
                "endIndex": end_idx
            },
            "properties": {"pixelSize": width_pixels},
            "fields": "pixelSize"
        }}]}
    ).execute()
    return {"success": True}


@mcp.tool()
def sheets_set_row_height(spreadsheet_id: str, sheet_name: str,
                   start_row: int, end_row: Optional[int] = None,
                   height_pixels: int = 21) -> Dict:
    """
    Set the pixel height of one or more rows.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_row: First row (1-based).
        end_row: Last row (1-based, inclusive). Defaults to same as start.
        height_pixels: Height in pixels.
    """
    if (err := require_editor("sheets_set_row_height")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    end_idx = end_row if end_row else start_row
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_row - 1,
                "endIndex": end_idx
            },
            "properties": {"pixelSize": height_pixels},
            "fields": "pixelSize"
        }}]}
    ).execute()
    return {"success": True}


@mcp.tool()
def sheets_auto_resize_columns(spreadsheet_id: str, sheet_name: str,
                        start_column: str, end_column: Optional[str] = None) -> Dict:
    """
    Auto-resize columns to fit their content.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_column: First column letter (e.g. 'A').
        end_column: Last column letter. Defaults to same as start.
    """
    if (err := require_editor("sheets_auto_resize_columns")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    start_idx = _col_index(start_column)
    end_idx = _col_index(end_column) + 1 if end_column else start_idx + 1
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_idx,
                "endIndex": end_idx
            }
        }}]}
    ).execute()
    return {"success": True}


@mcp.tool()
def sheets_hide_rows(spreadsheet_id: str, sheet_name: str,
              start_row: int, end_row: int) -> Dict:
    """
    Hide a range of rows.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_row: First row to hide (1-based).
        end_row: Last row to hide (1-based, inclusive).
    """
    if (err := require_editor("sheets_hide_rows")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_row - 1,
                "endIndex": end_row
            },
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser"
        }}]}
    ).execute()
    return {"success": True, "hidden": f"rows {start_row}-{end_row}"}


@mcp.tool()
def sheets_show_rows(spreadsheet_id: str, sheet_name: str,
              start_row: int, end_row: int) -> Dict:
    """Unhide rows."""
    if (err := require_editor("sheets_show_rows")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_row - 1,
                "endIndex": end_row
            },
            "properties": {"hiddenByUser": False},
            "fields": "hiddenByUser"
        }}]}
    ).execute()
    return {"success": True, "shown": f"rows {start_row}-{end_row}"}


@mcp.tool()
def sheets_hide_columns(spreadsheet_id: str, sheet_name: str,
                 start_column: str, end_column: str) -> Dict:
    """
    Hide columns.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_column: First column letter to hide (e.g. 'C').
        end_column: Last column letter to hide (inclusive).
    """
    if (err := require_editor("sheets_hide_columns")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    start_idx = _col_index(start_column)
    end_idx = _col_index(end_column) + 1
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_idx,
                "endIndex": end_idx
            },
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser"
        }}]}
    ).execute()
    return {"success": True, "hidden": f"columns {start_column}-{end_column}"}


@mcp.tool()
def sheets_show_columns(spreadsheet_id: str, sheet_name: str,
                 start_column: str, end_column: str) -> Dict:
    """Unhide columns."""
    if (err := require_editor("sheets_show_columns")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    start_idx = _col_index(start_column)
    end_idx = _col_index(end_column) + 1
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_idx,
                "endIndex": end_idx
            },
            "properties": {"hiddenByUser": False},
            "fields": "hiddenByUser"
        }}]}
    ).execute()
    return {"success": True, "shown": f"columns {start_column}-{end_column}"}


# ─────────────────────────────────────────────
# 6. CELL FORMATTING
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_format_range(
    spreadsheet_id: str,
    sheet_name: str,
    range: str,
    # Font
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    underline: Optional[bool] = None,
    strikethrough: Optional[bool] = None,
    font_family: Optional[str] = None,
    font_size: Optional[int] = None,
    font_color: Optional[str] = None,
    # Background
    background_color: Optional[str] = None,
    # Alignment
    horizontal_alignment: Optional[str] = None,
    vertical_alignment: Optional[str] = None,
    wrap_strategy: Optional[str] = None,
    # Number format
    number_format_type: Optional[str] = None,
    number_format_pattern: Optional[str] = None
) -> Dict:
    """
    Apply comprehensive formatting to a cell range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range (e.g. 'A1:D10' or 'A:A' or '1:1').
        bold: True/False.
        italic: True/False.
        underline: True/False.
        strikethrough: True/False.
        font_family: Font name (e.g. 'Arial', 'Roboto', 'Courier New').
        font_size: Font size in points.
        font_color: Text color as '#RRGGBB'.
        background_color: Background color as '#RRGGBB'.
        horizontal_alignment: 'LEFT', 'CENTER', 'RIGHT'.
        vertical_alignment: 'TOP', 'MIDDLE', 'BOTTOM'.
        wrap_strategy: 'OVERFLOW_CELL', 'LEGACY_WRAP', 'CLIP', 'WRAP'.
        number_format_type: 'TEXT', 'NUMBER', 'PERCENT', 'CURRENCY', 'DATE',
                            'TIME', 'DATE_TIME', 'SCIENTIFIC'.
        number_format_pattern: Custom format pattern (e.g. '#,##0.00', 'dd/mm/yyyy').
    """
    if (err := require_editor("sheets_format_range")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}

    cell_format = {}
    fields = []

    # Text format
    text_format = {}
    if bold is not None:
        text_format['bold'] = bold
    if italic is not None:
        text_format['italic'] = italic
    if underline is not None:
        text_format['underline'] = underline
    if strikethrough is not None:
        text_format['strikethrough'] = strikethrough
    if font_family is not None:
        text_format['fontFamily'] = font_family
    if font_size is not None:
        text_format['fontSize'] = font_size
    if font_color is not None:
        text_format['foregroundColorStyle'] = {"rgbColor": _color(font_color)}

    if text_format:
        cell_format['textFormat'] = text_format
        for k in text_format:
            fields.append(f"userEnteredFormat.textFormat.{k}")

    # Background color
    if background_color is not None:
        cell_format['backgroundColorStyle'] = {"rgbColor": _color(background_color)}
        fields.append("userEnteredFormat.backgroundColorStyle")

    # Alignment
    if horizontal_alignment is not None:
        cell_format['horizontalAlignment'] = horizontal_alignment.upper()
        fields.append("userEnteredFormat.horizontalAlignment")
    if vertical_alignment is not None:
        cell_format['verticalAlignment'] = vertical_alignment.upper()
        fields.append("userEnteredFormat.verticalAlignment")
    if wrap_strategy is not None:
        cell_format['wrapStrategy'] = wrap_strategy.upper()
        fields.append("userEnteredFormat.wrapStrategy")

    # Number format
    if number_format_type or number_format_pattern:
        nf = {}
        if number_format_type:
            nf['type'] = number_format_type.upper()
        if number_format_pattern:
            nf['pattern'] = number_format_pattern
        cell_format['numberFormat'] = nf
        fields.append("userEnteredFormat.numberFormat")

    if not fields:
        return {"error": "No formatting options provided"}

    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"repeatCell": {
            "range": gr,
            "cell": {"userEnteredFormat": cell_format},
            "fields": ",".join(fields)
        }}]}
    ).execute()
    return {"success": True, "formattedRange": f"{sheet_name}!{range}"}


@mcp.tool()
def sheets_set_borders(
    spreadsheet_id: str,
    sheet_name: str,
    range: str,
    top: Optional[str] = None,
    bottom: Optional[str] = None,
    left: Optional[str] = None,
    right: Optional[str] = None,
    inner_horizontal: Optional[str] = None,
    inner_vertical: Optional[str] = None,
    color_hex: str = "#000000"
) -> Dict:
    """
    Set borders on a cell range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range.
        top/bottom/left/right/inner_horizontal/inner_vertical:
            Border style: 'SOLID', 'SOLID_MEDIUM', 'SOLID_THICK', 'DOTTED',
            'DASHED', 'DOUBLE', 'NONE'.
            Pass None to leave unchanged.
        color_hex: Border color '#RRGGBB'.
    """
    if (err := require_editor("sheets_set_borders")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}

    def border_obj(style):
        if style is None:
            return None
        return {"style": style.upper(), "colorStyle": {"rgbColor": _color(color_hex)}}

    borders = {}
    if top is not None:
        borders["top"] = border_obj(top)
    if bottom is not None:
        borders["bottom"] = border_obj(bottom)
    if left is not None:
        borders["left"] = border_obj(left)
    if right is not None:
        borders["right"] = border_obj(right)
    if inner_horizontal is not None:
        borders["innerHorizontal"] = border_obj(inner_horizontal)
    if inner_vertical is not None:
        borders["innerVertical"] = border_obj(inner_vertical)

    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateBorders": {**gr, **borders}}]}
    ).execute()
    return {"success": True}


@mcp.tool()
def sheets_merge_cells(spreadsheet_id: str, sheet_name: str, range: str,
                merge_type: str = "MERGE_ALL") -> Dict:
    """
    Merge cells in a range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range to merge (e.g. 'A1:C1').
        merge_type: 'MERGE_ALL', 'MERGE_COLUMNS' (merge each column), 'MERGE_ROWS' (merge each row).
    """
    if (err := require_editor("sheets_merge_cells")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"mergeCells": {
            "range": gr,
            "mergeType": merge_type.upper()
        }}]}
    ).execute()
    return {"success": True, "merged": f"{sheet_name}!{range}"}


@mcp.tool()
def sheets_unmerge_cells(spreadsheet_id: str, sheet_name: str, range: str) -> Dict:
    """
    Unmerge cells in a range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range.
    """
    if (err := require_editor("sheets_unmerge_cells")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"unmergeCells": {"range": gr}}]}
    ).execute()
    return {"success": True, "unmerged": f"{sheet_name}!{range}"}


@mcp.tool()
def sheets_clear_formatting(spreadsheet_id: str, sheet_name: str, range: str) -> Dict:
    """
    Remove all formatting from a range (keeps values).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range.
    """
    if (err := require_editor("sheets_clear_formatting")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"repeatCell": {
            "range": gr,
            "cell": {},
            "fields": "userEnteredFormat"
        }}]}
    ).execute()
    return {"success": True}


# ─────────────────────────────────────────────
# 7. FREEZE, GROUP, SORT, FILTER
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_freeze_panes(spreadsheet_id: str, sheet_name: str,
                 frozen_rows: int = 0, frozen_columns: int = 0) -> Dict:
    """
    Freeze rows and/or columns (like Excel freeze panes).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        frozen_rows: Number of rows to freeze from top (0 to unfreeze).
        frozen_columns: Number of columns to freeze from left (0 to unfreeze).
    """
    if (err := require_editor("sheets_freeze_panes")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {
                    "frozenRowCount": frozen_rows,
                    "frozenColumnCount": frozen_columns
                }
            },
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
        }}]}
    ).execute()
    return {"success": True, "frozenRows": frozen_rows, "frozenColumns": frozen_columns}


@mcp.tool()
def sheets_sort_range(spreadsheet_id: str, sheet_name: str, range: str,
               sort_specs: List[Dict[str, Any]]) -> Dict:
    """
    Sort a range of cells.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range to sort (e.g. 'A1:D100').
        sort_specs: List of sort keys, each dict with:
            - 'column': column letter (e.g. 'A') or 1-based column number
            - 'ascending': True/False (default True)

    Example sort_specs:
        [{"column": "B", "ascending": False}, {"column": "A", "ascending": True}]
    """
    if (err := require_editor("sheets_sort_range")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}

    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    specs = []
    for s in sort_specs:
        col = s.get('column', 'A')
        if isinstance(col, str):
            col_idx = _col_index(col)
        else:
            col_idx = int(col) - 1
        specs.append({
            "dimensionIndex": col_idx,
            "sortOrder": "ASCENDING" if s.get('ascending', True) else "DESCENDING"
        })

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"sortRange": {
            "range": gr,
            "sortSpecs": specs
        }}]}
    ).execute()
    return {"success": True, "sorted": f"{sheet_name}!{range}"}


@mcp.tool()
def sheets_set_basic_filter(spreadsheet_id: str, sheet_name: str,
                     range: Optional[str] = None) -> Dict:
    """
    Enable the auto-filter dropdown on a range (like Excel filter arrows).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range for the filter (e.g. 'A1:F1'). If None, applies to whole sheet.
    """
    if (err := require_editor("sheets_set_basic_filter")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"setBasicFilter": {"filter": {"range": gr}}}]}
    ).execute()
    return {"success": True}


@mcp.tool()
def sheets_clear_basic_filter(spreadsheet_id: str, sheet_name: str) -> Dict:
    """
    Remove the basic filter (auto-filter) from a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
    """
    if (err := require_editor("sheets_clear_basic_filter")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"clearBasicFilter": {"sheetId": sheet_id}}]}
    ).execute()
    return {"success": True}


@mcp.tool()
def sheets_group_rows(spreadsheet_id: str, sheet_name: str,
               start_row: int, end_row: int) -> Dict:
    """
    Group rows (collapse/expand like Excel grouping).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_row: First row (1-based).
        end_row: Last row (1-based, inclusive).
    """
    if (err := require_editor("sheets_group_rows")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addDimensionGroup": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_row - 1,
                "endIndex": end_row
            }
        }}]}
    ).execute()
    return {"success": True, "grouped": f"rows {start_row}-{end_row}"}


@mcp.tool()
def sheets_group_columns(spreadsheet_id: str, sheet_name: str,
                  start_column: str, end_column: str) -> Dict:
    """
    Group columns.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        start_column: First column letter.
        end_column: Last column letter (inclusive).
    """
    if (err := require_editor("sheets_group_columns")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    start_idx = _col_index(start_column)
    end_idx = _col_index(end_column) + 1
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addDimensionGroup": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_idx,
                "endIndex": end_idx
            }
        }}]}
    ).execute()
    return {"success": True, "grouped": f"columns {start_column}-{end_column}"}


# ─────────────────────────────────────────────
# 8. CONDITIONAL FORMATTING
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_add_conditional_format_cell_value(
    spreadsheet_id: str,
    sheet_name: str,
    range: str,
    condition_type: str,
    condition_values: List[str],
    background_color: Optional[str] = None,
    font_color: Optional[str] = None,
    bold: Optional[bool] = None
) -> Dict:
    """
    Add conditional formatting based on cell value.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range to apply to (e.g. 'A1:A100').
        condition_type: Condition type:
            'NUMBER_GREATER', 'NUMBER_GREATER_THAN_EQ', 'NUMBER_LESS',
            'NUMBER_LESS_THAN_EQ', 'NUMBER_EQ', 'NUMBER_NOT_EQ',
            'NUMBER_BETWEEN', 'NUMBER_NOT_BETWEEN',
            'TEXT_CONTAINS', 'TEXT_NOT_CONTAINS', 'TEXT_STARTS_WITH',
            'TEXT_ENDS_WITH', 'TEXT_EQ',
            'DATE_EQ', 'DATE_BEFORE', 'DATE_AFTER',
            'BLANK', 'NOT_BLANK'.
        condition_values: List of value(s) for the condition (e.g. ['100'] or ['10','20'] for BETWEEN).
        background_color: Fill color '#RRGGBB'.
        font_color: Text color '#RRGGBB'.
        bold: True/False.
    """
    if (err := require_editor("sheets_add_conditional_format_cell_value")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}

    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    format_obj = {}
    if background_color:
        format_obj['backgroundColorStyle'] = {"rgbColor": _color(background_color)}
    if font_color or bold is not None:
        tf = {}
        if font_color:
            tf['foregroundColorStyle'] = {"rgbColor": _color(font_color)}
        if bold is not None:
            tf['bold'] = bold
        format_obj['textFormat'] = tf

    cond_vals = [{"userEnteredValue": v} for v in condition_values]

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addConditionalFormatRule": {
            "rule": {
                "ranges": [gr],
                "booleanRule": {
                    "condition": {
                        "type": condition_type.upper(),
                        "values": cond_vals
                    },
                    "format": format_obj
                }
            },
            "index": 0
        }}]}
    ).execute()
    return {"success": True, "conditionalFormat": f"{sheet_name}!{range}"}


@mcp.tool()
def sheets_add_conditional_format_color_scale(
    spreadsheet_id: str,
    sheet_name: str,
    range: str,
    min_color: str = "#FF0000",
    mid_color: Optional[str] = None,
    max_color: str = "#00FF00"
) -> Dict:
    """
    Add a color scale conditional format (gradient from min to max value).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range.
        min_color: Color for minimum values (default red '#FF0000').
        mid_color: Optional midpoint color.
        max_color: Color for maximum values (default green '#00FF00').
    """
    if (err := require_editor("sheets_add_conditional_format_color_scale")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}

    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    gradient = {
        "minpoint": {"colorStyle": {"rgbColor": _color(min_color)}, "type": "MIN"},
        "maxpoint": {"colorStyle": {"rgbColor": _color(max_color)}, "type": "MAX"}
    }
    if mid_color:
        gradient["midpoint"] = {"colorStyle": {"rgbColor": _color(mid_color)}, "type": "PERCENTILE",
                                 "value": "50"}

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addConditionalFormatRule": {
            "rule": {
                "ranges": [gr],
                "gradientRule": gradient
            },
            "index": 0
        }}]}
    ).execute()
    return {"success": True}


@mcp.tool()
def sheets_delete_conditional_formats(spreadsheet_id: str, sheet_name: str) -> Dict:
    """
    Delete ALL conditional formatting rules from a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
    """
    if (err := require_editor("sheets_delete_conditional_formats")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    # Get current rules count
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_data = next((s for s in meta.get('sheets', []) if s['properties']['sheetId'] == sheet_id), None)
    rules = sheet_data.get('conditionalFormats', []) if sheet_data else []
    requests = [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
        for _ in rules
    ]
    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests}
        ).execute()
    return {"success": True, "deletedRules": len(rules)}


# ─────────────────────────────────────────────
# 9. DATA VALIDATION
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_set_data_validation_list(
    spreadsheet_id: str,
    sheet_name: str,
    range: str,
    options: List[str],
    show_dropdown: bool = True,
    strict: bool = True,
    input_message: Optional[str] = None
) -> Dict:
    """
    Add a dropdown list validation to cells.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range to apply validation to.
        options: List of allowed values (e.g. ['Yes', 'No', 'Maybe']).
        show_dropdown: Show dropdown arrow.
        strict: Reject values not in list.
        input_message: Optional helper message shown to user.
    """
    if (err := require_editor("sheets_set_data_validation_list")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}

    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    rule = {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [{"userEnteredValue": o} for o in options]
        },
        "showCustomUi": show_dropdown,
        "strict": strict
    }
    if input_message:
        rule["inputMessage"] = input_message

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"setDataValidation": {
            "range": gr,
            "rule": rule
        }}]}
    ).execute()
    return {"success": True, "validation": "dropdown list", "range": f"{sheet_name}!{range}"}


@mcp.tool()
def sheets_set_data_validation_number(
    spreadsheet_id: str,
    sheet_name: str,
    range: str,
    condition_type: str,
    values: List[str],
    strict: bool = True,
    input_message: Optional[str] = None
) -> Dict:
    """
    Add number-based validation to cells.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range.
        condition_type: 'NUMBER_GREATER', 'NUMBER_LESS', 'NUMBER_BETWEEN',
                        'NUMBER_EQ', 'NUMBER_NOT_EQ', 'NUMBER_GREATER_THAN_EQ',
                        'NUMBER_LESS_THAN_EQ'.
        values: One or two values (e.g. ['0', '100'] for BETWEEN).
        strict: Reject invalid values.
        input_message: Helper message.
    """
    if (err := require_editor("sheets_set_data_validation_number")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}

    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    rule = {
        "condition": {
            "type": condition_type.upper(),
            "values": [{"userEnteredValue": v} for v in values]
        },
        "strict": strict
    }
    if input_message:
        rule["inputMessage"] = input_message

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"setDataValidation": {"range": gr, "rule": rule}}]}
    ).execute()
    return {"success": True, "validation": "number", "range": f"{sheet_name}!{range}"}


@mcp.tool()
def sheets_delete_data_validation(spreadsheet_id: str, sheet_name: str, range: str) -> Dict:
    """
    Remove data validation from a range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range.
    """
    if (err := require_editor("sheets_delete_data_validation")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"setDataValidation": {"range": gr}}]}
    ).execute()
    return {"success": True, "removed": f"{sheet_name}!{range}"}


# ─────────────────────────────────────────────
# 10. NAMED RANGES
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_list_named_ranges(spreadsheet_id: str) -> List[Dict]:
    """
    List all named ranges in a spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
    """
    sheets = _sheets_service()
    result = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return result.get('namedRanges', [])


@mcp.tool()
def sheets_create_named_range(spreadsheet_id: str, name: str,
                       sheet_name: str, range: str) -> Dict:
    """
    Create a named range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        name: Name for the range (e.g. 'SalesData').
        sheet_name: Sheet name.
        range: A1 range (e.g. 'A1:D100').
    """
    if (err := require_editor("sheets_create_named_range")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    result = sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addNamedRange": {
            "namedRange": {"name": name, "range": gr}
        }}]}
    ).execute()
    nr = result['replies'][0]['addNamedRange']['namedRange']
    return {"namedRangeId": nr['namedRangeId'], "name": name}


@mcp.tool()
def sheets_delete_named_range(spreadsheet_id: str, named_range_id: str) -> Dict:
    """
    Delete a named range by its ID.

    Args:
        spreadsheet_id: The spreadsheet ID.
        named_range_id: The ID of the named range (from sheets_list_named_ranges).
    """
    if (err := require_editor("sheets_delete_named_range")): return err
    sheets = _sheets_service()
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"deleteNamedRange": {"namedRangeId": named_range_id}}]}
    ).execute()
    return {"success": True, "deleted": named_range_id}


# ─────────────────────────────────────────────
# 11. PROTECTION
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_protect_range(spreadsheet_id: str, sheet_name: str, range: str,
                  description: str = "",
                  warning_only: bool = False,
                  editors: Optional[List[str]] = None) -> Dict:
    """
    Protect a range from editing.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range to protect.
        description: Description of the protection.
        warning_only: If True, shows warning but allows editing.
        editors: List of email addresses that CAN edit the protected range.
    """
    if (err := require_editor("sheets_protect_range")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    prot = {
        "range": gr,
        "description": description,
        "warningOnly": warning_only
    }
    if editors:
        prot["editors"] = {"users": editors}
    result = sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addProtectedRange": {"protectedRange": prot}}]}
    ).execute()
    pr = result['replies'][0]['addProtectedRange']['protectedRange']
    return {"protectedRangeId": pr['protectedRangeId'], "range": f"{sheet_name}!{range}"}


@mcp.tool()
def sheets_list_protected_ranges(spreadsheet_id: str, sheet_name: Optional[str] = None) -> List[Dict]:
    """
    List all protected ranges in a spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Optional filter by sheet name.
    """
    sheets = _sheets_service()
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    result = []
    for s in meta.get('sheets', []):
        if sheet_name and s['properties']['title'] != sheet_name:
            continue
        for pr in s.get('protectedRanges', []):
            result.append({
                "protectedRangeId": pr.get('protectedRangeId'),
                "description": pr.get('description', ''),
                "sheet": s['properties']['title'],
                "warningOnly": pr.get('warningOnly', False)
            })
    return result


@mcp.tool()
def sheets_remove_protection(spreadsheet_id: str, protected_range_id: int) -> Dict:
    """
    Remove a protection rule.

    Args:
        spreadsheet_id: The spreadsheet ID.
        protected_range_id: The ID from sheets_list_protected_ranges.
    """
    if (err := require_editor("sheets_remove_protection")): return err
    sheets = _sheets_service()
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"deleteProtectedRange": {"protectedRangeId": protected_range_id}}]}
    ).execute()
    return {"success": True, "removed": protected_range_id}


# ─────────────────────────────────────────────
# 12. NOTES / COMMENTS
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_add_note(spreadsheet_id: str, sheet_name: str, cell: str,
             note: str) -> Dict:
    """
    Add a note (comment) to a cell.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        cell: Cell reference (e.g. 'A1').
        note: Note text.
    """
    if (err := require_editor("sheets_add_note")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, cell)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"repeatCell": {
            "range": gr,
            "cell": {"note": note},
            "fields": "note"
        }}]}
    ).execute()
    return {"success": True, "note": note, "cell": f"{sheet_name}!{cell}"}


@mcp.tool()
def sheets_clear_notes(spreadsheet_id: str, sheet_name: str, range: str) -> Dict:
    """
    Remove notes from a cell range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        range: A1 range.
    """
    if (err := require_editor("sheets_clear_notes")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}
    gr = _range_to_grid(spreadsheet_id, sheet_id, range)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"repeatCell": {
            "range": gr,
            "cell": {"note": ""},
            "fields": "note"
        }}]}
    ).execute()
    return {"success": True}


# ─────────────────────────────────────────────
# 13. CHARTS
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_add_chart(
    spreadsheet_id: str,
    sheet_name: str,
    chart_type: str,
    data_range: str,
    title: str = "",
    x_axis_label: str = "",
    y_axis_label: str = "",
    position_row: int = 0,
    position_col: int = 0,
    width: int = 600,
    height: int = 400
) -> Dict:
    """
    Add a chart to a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        chart_type: Chart type:
            'BAR', 'LINE', 'AREA', 'COLUMN', 'SCATTER', 'PIE', 'DONUT',
            'COMBO', 'STEPPED_AREA', 'BUBBLE', 'CANDLESTICK', 'WATERFALL'.
        data_range: A1 range for chart data (e.g. 'A1:C10').
        title: Chart title.
        x_axis_label: X-axis label.
        y_axis_label: Y-axis label.
        position_row: Row position (0-based) of top-left corner.
        position_col: Column position (0-based) of top-left corner.
        width: Chart width in pixels.
        height: Chart height in pixels.
    """
    if (err := require_editor("sheets_add_chart")): return err
    sheets = _sheets_service()
    sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
    if sheet_id is None:
        return {"error": f"Sheet '{sheet_name}' not found"}

    data_gr = _range_to_grid(spreadsheet_id, sheet_id, data_range)
    source = {"sheetId": sheet_id,
              "startRowIndex": data_gr.get("startRowIndex", 0),
              "endRowIndex": data_gr.get("endRowIndex", 100),
              "startColumnIndex": data_gr.get("startColumnIndex", 0),
              "endColumnIndex": data_gr.get("endColumnIndex", 5)}

    chart_type_upper = chart_type.upper()

    if chart_type_upper == "PIE":
        spec = {
            "title": title,
            "pieChart": {
                "legendPosition": "RIGHT_LEGEND",
                "domain": {"sourceRange": {"sources": [source]}},
                "series": {"sourceRange": {"sources": [source]}}
            }
        }
    else:
        axis = []
        if x_axis_label:
            axis.append({"position": "BOTTOM_AXIS", "title": x_axis_label})
        if y_axis_label:
            axis.append({"position": "LEFT_AXIS", "title": y_axis_label})
        spec = {
            "title": title,
            "basicChart": {
                "chartType": chart_type_upper,
                "legendPosition": "BOTTOM_LEGEND",
                "axis": axis,
                "domains": [{"domain": {"sourceRange": {"sources": [source]}}}],
                "series": [{"series": {"sourceRange": {"sources": [source]}},
                            "targetAxis": "LEFT_AXIS"}]
            }
        }

    result = sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addChart": {
            "chart": {
                "spec": spec,
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId": sheet_id,
                            "rowIndex": position_row,
                            "columnIndex": position_col
                        },
                        "widthPixels": width,
                        "heightPixels": height
                    }
                }
            }
        }}]}
    ).execute()
    chart_id = result['replies'][0]['addChart']['chart']['chartId']
    return {"success": True, "chartId": chart_id, "title": title}


@mcp.tool()
def sheets_delete_chart(spreadsheet_id: str, chart_id: int) -> Dict:
    """
    Delete a chart by its ID.

    Args:
        spreadsheet_id: The spreadsheet ID.
        chart_id: Chart ID (from sheets_add_chart or sheets_list_charts).
    """
    if (err := require_editor("sheets_delete_chart")): return err
    sheets = _sheets_service()
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"deleteEmbeddedObject": {"objectId": chart_id}}]}
    ).execute()
    return {"success": True, "deleted": chart_id}


@mcp.tool()
def sheets_list_charts(spreadsheet_id: str, sheet_name: Optional[str] = None) -> List[Dict]:
    """
    List all charts in a spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Optional filter by sheet name.
    """
    sheets = _sheets_service()
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    result = []
    for s in meta.get('sheets', []):
        if sheet_name and s['properties']['title'] != sheet_name:
            continue
        for chart in s.get('charts', []):
            result.append({
                "chartId": chart['chartId'],
                "sheet": s['properties']['title'],
                "title": chart.get('spec', {}).get('title', ''),
                "type": list(chart.get('spec', {}).keys())
            })
    return result


# ─────────────────────────────────────────────
# 14. FIND & REPLACE
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_find_and_replace(
    spreadsheet_id: str,
    find: str,
    replacement: str,
    sheet_name: Optional[str] = None,
    match_case: bool = False,
    match_entire_cell: bool = False,
    search_by_regex: bool = False,
    include_formulas: bool = False
) -> Dict:
    """
    Find and replace text across the spreadsheet or a specific sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        find: Text to find (or regex pattern if search_by_regex=True).
        replacement: Replacement text.
        sheet_name: Limit to this sheet (optional).
        match_case: Case-sensitive search.
        match_entire_cell: Match whole cell content only.
        search_by_regex: Treat 'find' as a regular expression.
        include_formulas: Search within formulas too.
    """
    if (err := require_editor("sheets_find_and_replace")): return err
    sheets = _sheets_service()
    req = {
        "find": find,
        "replacement": replacement,
        "matchCase": match_case,
        "matchEntireCell": match_entire_cell,
        "searchByRegex": search_by_regex,
        "includeFormulas": include_formulas,
        "allSheets": sheet_name is None
    }
    if sheet_name:
        sheet_id = _get_sheet_id(sheets, spreadsheet_id, sheet_name)
        if sheet_id is None:
            return {"error": f"Sheet '{sheet_name}' not found"}
        req["sheetId"] = sheet_id

    result = sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"findReplace": req}]}
    ).execute()
    r = result['replies'][0].get('findReplace', {})
    return {
        "occurrencesChanged": r.get('occurrencesChanged', 0),
        "valuesChanged": r.get('valuesChanged', 0)
    }


# ─────────────────────────────────────────────
# 15. PIVOT TABLES
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_create_pivot_table(
    spreadsheet_id: str,
    source_sheet: str,
    source_range: str,
    dest_sheet: str,
    dest_cell: str,
    rows: List[Dict],
    values: List[Dict],
    columns: Optional[List[Dict]] = None
) -> Dict:
    """
    Create a pivot table.

    Args:
        spreadsheet_id: The spreadsheet ID.
        source_sheet: Sheet containing source data.
        source_range: A1 range of source data (e.g. 'A1:E100').
        dest_sheet: Sheet to place the pivot table.
        dest_cell: Top-left cell for pivot table (e.g. 'A1').
        rows: List of row groupings. Each dict:
            {'sourceColumnOffset': 0, 'showTotals': True, 'sortOrder': 'ASCENDING'}
        values: List of value summaries. Each dict:
            {'sourceColumnOffset': 3, 'summarizeFunction': 'SUM', 'name': 'Total Sales'}
        columns: Optional list of column groupings (same format as rows).

    summarizeFunction options: SUM, COUNTA, COUNT, COUNTUNIQUE, AVERAGE, MAX, MIN, MEDIAN, PRODUCT, STDEV, STDEVP, VAR, VARP, CUSTOM
    """
    if (err := require_editor("sheets_create_pivot_table")): return err
    sheets = _sheets_service()
    src_sheet_id = _get_sheet_id(sheets, spreadsheet_id, source_sheet)
    dst_sheet_id = _get_sheet_id(sheets, spreadsheet_id, dest_sheet)
    if src_sheet_id is None:
        return {"error": f"Source sheet '{source_sheet}' not found"}
    if dst_sheet_id is None:
        return {"error": f"Destination sheet '{dest_sheet}' not found"}

    src_gr = _range_to_grid(spreadsheet_id, src_sheet_id, source_range)
    dst_gr = _range_to_grid(spreadsheet_id, dst_sheet_id, dest_cell)

    pivot = {
        "source": src_gr,
        "rows": rows,
        "values": values
    }
    if columns:
        pivot["columns"] = columns

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"updateCells": {
            "rows": [{"values": [{"pivotTable": pivot}]}],
            "fields": "pivotTable",
            "start": {
                "sheetId": dst_sheet_id,
                "rowIndex": dst_gr.get("startRowIndex", 0),
                "columnIndex": dst_gr.get("startColumnIndex", 0)
            }
        }}]}
    ).execute()
    return {"success": True, "pivotAt": f"{dest_sheet}!{dest_cell}"}


# ─────────────────────────────────────────────
# 16. HYPERLINKS & SPECIAL CELL VALUES
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_set_hyperlink(spreadsheet_id: str, sheet_name: str, cell: str,
                  url: str, display_text: Optional[str] = None) -> Dict:
    """
    Set a hyperlink in a cell using a HYPERLINK formula.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Sheet name.
        cell: Cell reference (e.g. 'A1').
        url: URL for the hyperlink.
        display_text: Text to display (defaults to URL).
    """
    if (err := require_editor("sheets_set_hyperlink")): return err
    text = display_text or url
    formula = f'=HYPERLINK("{url}","{text}")'
    sheets = _sheets_service()
    full_range = f"{sheet_name}!{cell}"
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=full_range,
        valueInputOption="USER_ENTERED",
        body={"values": [[formula]]}
    ).execute()
    return {"success": True, "cell": f"{sheet_name}!{cell}", "url": url}


# ─────────────────────────────────────────────
# 17. BATCH RAW REQUESTS
# ─────────────────────────────────────────────

@mcp.tool()
def sheets_batch_requests(spreadsheet_id: str, requests: List[Dict]) -> Dict:
    """
    Send raw batchUpdate requests directly to the Sheets API.
    Use this for advanced operations not covered by other tools.

    Args:
        spreadsheet_id: The spreadsheet ID.
        requests: List of Google Sheets API request objects.

    See: https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/batchUpdate
    """
    if (err := require_editor("sheets_batch_requests")): return err
    sheets = _sheets_service()
    result = sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests}
    ).execute()
    return {"replies": result.get('replies', []), "spreadsheetId": result.get('spreadsheetId')}


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

