import os
import base64
import re
import json
import io
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
from mcp.server.fastmcp import FastMCP
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import database as db
import oauth_handler as oauth

# ── FastMCP server ──────────────────────────────────────────────────────────
mcp = FastMCP("Email_Drive_Agent")
mcp.settings.port = 9006

# Per-user service/data caches
gmail_services:     dict = {}   # {user_id: service}
drive_services:     dict = {}   # {user_id: service}
email_cache:        dict = {}   # {user_id: {email_id: email_data}}
attachment_cache:   dict = {}   # {user_id: {cache_key: attachment_info}}

# Local filesystem directories to search
LOCAL_SEARCH_DIRS = [
    str(Path.home() / "Documents"),
    str(Path.home() / "Downloads"),
    str(Path.home() / "Desktop"),
]


# ── Type-coercion helpers ───────────────────────────────────────────────────

def _to_user_id(raw) -> Optional[int]:
    """
    Convert any incoming user_id value (int or str) to a positive int.
    Returns None if the value is missing or not a valid positive integer.
    The LLM always sends tool parameters as strings, so we must handle both.
    """
    try:
        val = int(raw)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _to_int(raw, default: int) -> int:
    """Convert any incoming integer-like parameter, falling back to default."""
    try:
        val = int(raw)
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def _to_bool(raw) -> bool:
    """Convert any incoming boolean-like parameter."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return bool(raw)


# ── Shared service getters ──────────────────────────────────────────────────

def get_gmail_service(user_id):
    """Get (or lazily create) an authenticated Gmail service for a user."""
    uid = _to_user_id(user_id)
    if uid is None:
        raise ValueError("Invalid or missing user_id")
    if uid not in gmail_services:
        gmail_services[uid] = oauth.get_gmail_service(uid)
    return gmail_services[uid]


def get_drive_service(user_id):
    """Get (or lazily create) an authenticated Drive service for a user."""
    uid = _to_user_id(user_id)
    if uid is None:
        raise ValueError("Invalid or missing user_id")
    if uid not in drive_services:
        drive_services[uid] = oauth.get_drive_service(uid)
    return drive_services[uid]


# ── Utility functions ───────────────────────────────────────────────────────

def format_file_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def extract_body(payload) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    if "parts" in payload:
        for part in payload["parts"]:
            body = extract_body(part)
            if body:
                return body
    else:
        if "data" in payload.get("body", {}):
            try:
                return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8")
            except Exception:
                return "[Unable to decode body]"
    return ""


def get_date_query(time_filter: str) -> str:
    """Return a Gmail search query fragment for a named time window."""
    today = datetime.now().date()
    tf = time_filter.lower()
    if tf in ("today", "recent"):
        return f"after:{today.isoformat()}"
    elif tf == "yesterday":
        yesterday = today - timedelta(days=1)
        return f"after:{yesterday.isoformat()} before:{today.isoformat()}"
    elif tf == "this_week":
        week_start = today - timedelta(days=today.weekday())
        return f"after:{week_start.isoformat()}"
    elif tf == "last_7_days":
        return f"after:{(today - timedelta(days=7)).isoformat()}"
    return ""


def extract_attachments_detailed(payload, email_id: str, user_id) -> List[Dict]:
    """Extract attachment metadata from a Gmail message payload and cache it."""
    uid = _to_user_id(user_id)
    attachments: List[Dict] = []
    if uid is None:
        return attachments

    if uid not in attachment_cache:
        attachment_cache[uid] = {}

    def process_parts(parts, eid):
        for part in parts:
            if part.get("filename"):
                att_info = {
                    "filename":     part["filename"],
                    "mimeType":     part.get("mimeType", "unknown"),
                    "size":         part.get("body", {}).get("size", 0),
                    "attachmentId": part.get("body", {}).get("attachmentId", ""),
                    "emailId":      eid,
                }
                attachments.append(att_info)
                attachment_cache[uid][f"{eid}:{part['filename']}"] = att_info
            if "parts" in part:
                process_parts(part["parts"], eid)

    if "parts" in payload:
        process_parts(payload["parts"], email_id)

    return attachments


def search_local_files(query: str, max_results=10) -> List[Dict]:
    """Walk LOCAL_SEARCH_DIRS and return files whose names match the query keywords."""
    max_results = _to_int(max_results, 10)
    query_lower = query.lower()

    stop_words = {
        "the", "of", "a", "an", "and", "or", "in", "on", "at",
        "to", "for", "with", "by", "from", "file", "show", "me", "get", "find",
    }
    keywords = [w for w in query_lower.split() if w not in stop_words and len(w) > 2]
    if not keywords:
        keywords = [query_lower]

    results: List[Dict] = []
    for directory in LOCAL_SEARCH_DIRS:
        if not os.path.exists(directory):
            continue
        try:
            for root, dirs, files in os.walk(directory):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for filename in files:
                    fl = filename.lower()
                    score = sum(1 for kw in keywords if kw in fl)
                    if score > 0:
                        fp = os.path.join(root, filename)
                        try:
                            stat = os.stat(fp)
                            results.append({
                                "name":        filename,
                                "path":        fp,
                                "size":        stat.st_size,
                                "modified":    datetime.fromtimestamp(stat.st_mtime).isoformat(),
                                "type":        "local_file",
                                "match_score": score,
                            })
                        except (PermissionError, OSError):
                            continue
                    if len(results) >= max_results * 3:
                        break
                if len(results) >= max_results * 3:
                    break
        except (PermissionError, OSError):
            continue

    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results[:max_results]


def search_drive_files_helper(service, keywords: List[str], max_results=10) -> List[Dict]:
    """Internal Drive search by keyword list — used by smart_search_with_memory."""
    max_results = _to_int(max_results, 10)
    query_parts = []
    for kw in keywords:
        safe_kw = kw.replace("'", "\\'")
        query_parts.append(f"(name contains '{safe_kw}' or fullText contains '{safe_kw}')")
    query = " or ".join(query_parts) + " and trashed=false"

    print(f"🔍 Drive internal query: {query}")
    try:
        results = service.files().list(
            q=query,
            pageSize=max_results,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            orderBy="modifiedTime desc",
        ).execute()
        files = results.get("files", [])
        print(f"✅ Drive found {len(files)} files")
        return files
    except Exception as e:
        print(f"❌ Drive search error: {e}")
        return []


def _mime_icon(mime_type: str) -> str:
    if "folder" in mime_type:        return "📂"
    if "document" in mime_type or "pdf" in mime_type: return "📄"
    if "spreadsheet" in mime_type:   return "📊"
    if "presentation" in mime_type:  return "📙"
    if "image" in mime_type:         return "🖼️"
    return "📎"


# ── MCP Tools ───────────────────────────────────────────────────────────────

@mcp.tool()
def fetch_emails(user_id, max_results=10, time_filter: str = "all", unread_only=False) -> str:
    """Fetch recent emails for the logged-in user."""
    uid          = _to_user_id(user_id)
    max_results  = _to_int(max_results, 10)
    unread_only  = _to_bool(unread_only)

    if uid is None:
        return "❌ Invalid user_id — please log in again."

    try:
        service = get_gmail_service(uid)
        query_parts = []
        if unread_only:
            query_parts.append("is:unread")
        date_q = get_date_query(time_filter)
        if date_q:
            query_parts.append(date_q)

        results = service.users().messages().list(
            userId="me", q=" ".join(query_parts), maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return f"No emails found for filter: {time_filter}"

        if uid not in email_cache:
            email_cache[uid] = {}

        email_list = []
        for msg in messages:
            email = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            email_cache[uid][msg["id"]] = email
            headers     = {h["name"]: h["value"] for h in email.get("payload", {}).get("headers", [])}
            attachments = extract_attachments_detailed(email.get("payload", {}), msg["id"], uid)
            email_list.append({
                "id":          msg["id"],
                "subject":     headers.get("Subject", "No Subject"),
                "from":        headers.get("From", "Unknown"),
                "date":        headers.get("Date", "Unknown"),
                "snippet":     email.get("snippet", ""),
                "attachments": attachments,
            })

        output = f"📬 Found {len(email_list)} emails ({time_filter}):\n\n"
        for i, em in enumerate(email_list, 1):
            output += f"{i}. **{em['subject']}**\n"
            output += f"   From: {em['from']}\n"
            output += f"   Date: {em['date']}\n"
            output += f"   Email ID: `{em['id']}`\n"
            if em["attachments"]:
                output += f"   📎 Attachments ({len(em['attachments'])}):\n"
                for att in em["attachments"]:
                    output += f"      - **{att['filename']}** ({att['size']/1024:.2f} KB)\n"
            output += f"   Preview: {em['snippet'][:100]}...\n\n"
        return output

    except Exception as e:
        return f"❌ Error fetching emails: {str(e)}"


@mcp.tool()
def search_emails(user_id, query: str, max_results=10, time_filter: str = "all") -> str:
    """Search a user's Gmail by query string."""
    uid         = _to_user_id(user_id)
    max_results = _to_int(max_results, 10)

    if uid is None:
        return "❌ Invalid user_id — please log in again."

    try:
        service  = get_gmail_service(uid)
        date_q   = get_date_query(time_filter)
        full_q   = f"{query} {date_q}".strip()

        results  = service.users().messages().list(userId="me", q=full_q, maxResults=max_results).execute()
        messages = results.get("messages", [])
        if not messages:
            return f"No emails found matching: {query} ({time_filter})"

        if uid not in email_cache:
            email_cache[uid] = {}

        output = f"🔍 Found {len(messages)} emails for '{query}' ({time_filter}):\n\n"
        for i, msg in enumerate(messages, 1):
            email = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            email_cache[uid][msg["id"]] = email
            headers     = {h["name"]: h["value"] for h in email.get("payload", {}).get("headers", [])}
            attachments = extract_attachments_detailed(email.get("payload", {}), msg["id"], uid)
            output += f"{i}. **{headers.get('Subject', 'No Subject')}**\n"
            output += f"   From: {headers.get('From', 'Unknown')}\n"
            output += f"   Date: {headers.get('Date', 'Unknown')}\n"
            output += f"   Email ID: `{msg['id']}`\n"
            if attachments:
                output += f"   📎 {len(attachments)} attachment(s)\n"
            output += "\n"
        return output

    except Exception as e:
        return f"❌ Error searching emails: {str(e)}"


@mcp.tool()
def download_attachment(user_id, email_id: str, filename: str, attachment_id: str = None) -> str:
    """Download a specific email attachment for a user."""
    uid = _to_user_id(user_id)
    if uid is None:
        return "❌ Invalid user_id — please log in again."

    try:
        service = get_gmail_service(uid)

        # Try to find attachment_id from cache if not supplied
        if not attachment_id and uid in attachment_cache:
            cached = attachment_cache[uid].get(f"{email_id}:{filename}")
            if cached:
                attachment_id = cached["attachmentId"]

        if not attachment_id:
            return f"❌ Could not find attachment '{filename}' in email `{email_id}`"

        attachment = service.users().messages().attachments().get(
            userId="me", messageId=email_id, id=attachment_id
        ).execute()

        file_data = base64.urlsafe_b64decode(attachment["data"])
        save_path = oauth.get_user_attachments_path(uid)
        file_path = os.path.join(save_path, filename)

        with open(file_path, "wb") as f:
            f.write(file_data)

        db.save_download_record(uid, filename, file_path, len(file_data))
        return f"✅ Downloaded: {filename}\n\nLocation: {file_path}\nSize: {len(file_data)/1024:.2f} KB"

    except Exception as e:
        return f"❌ Error downloading attachment: {str(e)}"


@mcp.tool()
def list_drive_files(user_id, max_results=10, query: str = None) -> str:
    """List files from the user's Google Drive."""
    uid         = _to_user_id(user_id)
    max_results = _to_int(max_results, 10)

    if uid is None:
        return "❌ Invalid user_id — please log in again."

    try:
        service     = get_drive_service(uid)
        drive_query = query if query else "trashed=false"

        results = service.files().list(
            pageSize=max_results,
            q=drive_query,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)",
            orderBy="modifiedTime desc",
        ).execute()
        files = results.get("files", [])

        if not files:
            return "📁 No files found in Drive"

        output = f"📁 **Found {len(files)} files in Drive:**\n\n"
        for i, file in enumerate(files, 1):
            name      = file.get("name", "Unnamed")
            file_id   = file.get("id", "")
            mime_type = file.get("mimeType", "unknown")
            size      = int(file.get("size", 0)) if "size" in file else 0
            modified  = file.get("modifiedTime", "Unknown")
            icon      = _mime_icon(mime_type)

            output += f"{i}. {icon} **{name}**\n"
            output += f"   File ID: `{file_id}`\n"
            if size > 0:
                output += f"   Size: {format_file_size(size)}\n"
            output += f"   Modified: {modified}\n\n"
        return output

    except Exception as e:
        return f"❌ Error listing Drive files: {str(e)}"


@mcp.tool()
def search_drive_files(user_id, query: str, max_results=20) -> str:
    """Search files in the user's Google Drive by name and content."""
    uid         = _to_user_id(user_id)
    max_results = _to_int(max_results, 20)

    if uid is None:
        return "❌ Invalid user_id — please log in again."

    try:
        service = get_drive_service(uid)
        stop_words = {
            "the", "of", "a", "an", "and", "or", "in", "on", "at",
            "to", "for", "with", "by", "from", "file", "show", "me",
            "get", "find", "search", "my", "can", "you", "u",
        }
        keywords = [w.lower() for w in query.split() if w.lower() not in stop_words and len(w) > 2]
        if not keywords:
            keywords = [query.lower()]

        query_parts = []
        for kw in keywords:
            safe = kw.replace("'", "\\'")
            query_parts.append(f"(name contains '{safe}' or fullText contains '{safe}')")
        drive_query = " or ".join(query_parts) + " and trashed=false"

        print(f"🔍 Drive search query: {drive_query}")
        results = service.files().list(
            q=drive_query,
            pageSize=max_results,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)",
            orderBy="modifiedTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = results.get("files", [])

        if not files:
            return (
                f"🔍 No files found matching '{query}' in Drive\n\n"
                f"Tried keywords: {', '.join(keywords)}\n\n"
                "💡 Try 'list drive files' to see all files, or use different keywords."
            )

        output = f"🔍 **Found {len(files)} files matching '{query}' in Drive:**\n\n"
        for i, file in enumerate(files, 1):
            name      = file.get("name", "Unnamed")
            file_id   = file.get("id", "")
            size      = int(file.get("size", 0)) if "size" in file else 0
            modified  = file.get("modifiedTime", "Unknown")
            mime_type = file.get("mimeType", "")
            icon      = _mime_icon(mime_type)
            matched   = [kw for kw in keywords if kw in name.lower()]

            output += f"{i}. {icon} **{name}**\n"
            output += f"   File ID: `{file_id}`\n"
            if size > 0:
                output += f"   Size: {format_file_size(size)}\n"
            output += f"   Modified: {modified}\n"
            if matched:
                output += f"   🎯 Matches: {', '.join(matched)}\n"
            output += "\n"

        output += "💡 To download, say: **download drive file [file_id]**"
        return output

    except Exception as e:
        return f"❌ Error searching Drive files: {str(e)}"


@mcp.tool()
def download_drive_file(user_id, file_id: str, filename: str = None) -> str:
    """Download a file from the user's Google Drive."""
    uid = _to_user_id(user_id)
    if uid is None:
        return "❌ Invalid user_id — please log in again."

    try:
        service       = get_drive_service(uid)
        file_metadata = service.files().get(fileId=file_id, fields="name,mimeType,size").execute()

        if not filename:
            filename = file_metadata.get("name", f"drive_file_{file_id}")

        mime_type = file_metadata.get("mimeType", "")
        export_mimetypes = {
            "application/vnd.google-apps.document":     ("application/pdf", ".pdf"),
            "application/vnd.google-apps.spreadsheet":  (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"
            ),
            "application/vnd.google-apps.presentation": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"
            ),
        }

        save_path = oauth.get_user_attachments_path(uid)
        if mime_type in export_mimetypes:
            export_mime, ext = export_mimetypes[mime_type]
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            if not filename.endswith(ext):
                filename += ext
        else:
            request = service.files().get_media(fileId=file_id)

        file_path = os.path.join(save_path, filename)
        fh = io.FileIO(file_path, "wb")
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.close()

        file_size = os.path.getsize(file_path)
        db.save_download_record(uid, filename, file_path, file_size)
        return (
            f"✅ Successfully downloaded: {filename}\n\n"
            f"Location: {file_path}\nSize: {format_file_size(file_size)}"
        )

    except Exception as e:
        return f"❌ Error downloading Drive file: {str(e)}"


@mcp.tool()
def get_drive_storage_info(user_id) -> str:
    """Return the user's Google Drive storage usage."""
    uid = _to_user_id(user_id)
    if uid is None:
        return "❌ Invalid user_id — please log in again."

    try:
        service   = get_drive_service(uid)
        about     = service.about().get(fields="storageQuota,user").execute()
        quota     = about.get("storageQuota", {})
        user_info = about.get("user", {})

        limit          = int(quota.get("limit", 0))
        usage          = int(quota.get("usage", 0))
        usage_in_drive = int(quota.get("usageInDrive", 0))

        output  = "**📊 Drive Storage Information**\n\n"
        output += f"**User:** {user_info.get('emailAddress', 'Unknown')}\n\n"
        if limit > 0:
            pct     = (usage / limit) * 100
            output += f"**Total Usage:** {format_file_size(usage)} / {format_file_size(limit)} ({pct:.1f}%)\n"
            output += f"**In Drive:** {format_file_size(usage_in_drive)}\n"
            output += f"**Available:** {format_file_size(limit - usage)}\n"
        else:
            output += f"**Storage:** Unlimited\n"
            output += f"**Current Usage:** {format_file_size(usage)}\n"
        return output

    except Exception as e:
        return f"❌ Error getting storage info: {str(e)}"


@mcp.tool()
def smart_search_with_memory(user_id, query: str, max_results=10) -> str:
    """Search across local files, Gmail attachments, AND Google Drive. Returns numbered results."""
    uid         = _to_user_id(user_id)
    max_results = _to_int(max_results, 10)

    if uid is None:
        return "❌ Invalid user_id — please log in again."

    output = f"🔍 **Searching for:** '{query}'\n\n"
    stop_words = {
        "the", "of", "a", "an", "and", "or", "in", "on", "at",
        "to", "for", "with", "by", "from", "file", "show", "me",
        "get", "find", "search", "can", "you", "u",
    }
    keywords = [w.lower() for w in query.split() if w.lower() not in stop_words and len(w) > 2]
    if not keywords:
        keywords = [query.lower()]

    print(f"🔍 Search keywords: {keywords}")
    all_results: List[Dict] = []
    result_index = 1

    # ── 1. Local files ──────────────────────────────────────────────────────
    output += "**📁 Local Files:**\n"
    local_files = search_local_files(query, max_results)
    for file in local_files:
        output += f"**{result_index}.** {file['name']}\n"
        output += f"   📂 `{file['path']}`\n"
        output += f"   💾 {file['size']/1024:.2f} KB\n\n"
        all_results.append({
            "number": result_index, "type": "local",
            "name": file["name"], "path": file["path"], "size": file["size"],
        })
        result_index += 1
    if not local_files:
        output += "   No local files found.\n\n"

    # ── 2. Gmail attachments ────────────────────────────────────────────────
    output += "**📧 Email Attachments:**\n"
    try:
        gmail_service = get_gmail_service(uid)
        gmail_query   = f"has:attachment ({' OR '.join(keywords)})"
        results       = gmail_service.users().messages().list(
            userId="me", q=gmail_query, maxResults=max_results
        ).execute()
        messages = results.get("messages", [])

        if uid not in email_cache:
            email_cache[uid] = {}

        found = False
        for msg in messages:
            email = gmail_service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            email_cache[uid][msg["id"]] = email
            headers     = {h["name"]: h["value"] for h in email.get("payload", {}).get("headers", [])}
            attachments = extract_attachments_detailed(email.get("payload", {}), msg["id"], uid)
            for att in attachments:
                if any(kw in att["filename"].lower() for kw in keywords):
                    found = True
                    output += f"**{result_index}.** {att['filename']}\n"
                    output += f"   📧 From: {headers.get('Subject', 'No Subject')}\n"
                    output += f"   💾 {att['size']/1024:.2f} KB\n\n"
                    all_results.append({
                        "number":        result_index,
                        "type":          "email",
                        "name":          att["filename"],
                        "email_id":      msg["id"],
                        "attachment_id": att["attachmentId"],
                        "size":          att["size"],
                    })
                    result_index += 1
        if not found:
            output += "   No email attachments found.\n\n"

    except Exception as e:
        output += f"   ❌ Gmail error: {str(e)}\n\n"

    # ── 3. Google Drive ─────────────────────────────────────────────────────
    output += "**☁️ Google Drive Files:**\n"
    try:
        drive_service = oauth.get_drive_service(uid)
        drive_files   = search_drive_files_helper(drive_service, keywords, max_results)

        if not drive_files:
            output += "   No Drive files found.\n\n"
        else:
            for file in drive_files:
                size_kb   = int(file.get("size", 0)) / 1024 if file.get("size") else 0
                mime_type = file.get("mimeType", "")
                icon      = _mime_icon(mime_type)
                output += f"**{result_index}.** {icon} {file['name']}\n"
                output += f"   ☁️ Drive File ID: `{file['id']}`\n"
                if size_kb > 0:
                    output += f"   💾 {size_kb:.2f} KB\n"
                output += "\n"
                all_results.append({
                    "number":   result_index,
                    "type":     "drive",
                    "name":     file["name"],
                    "file_id":  file["id"],
                    "mimeType": file["mimeType"],
                    "size":     file.get("size", 0),
                })
                result_index += 1

    except Exception as e:
        output += f"   ❌ Drive error: {str(e)}\n\n"

    # ── Cache results + summary ─────────────────────────────────────────────
    db.save_search_cache(uid, "last_search", json.dumps(all_results))
    output += "---\n\n"
    total = len(all_results)

    if total == 0:
        output += f"❌ No files found for '{query}'\n"
        output += f"💡 Tried keywords: {', '.join(keywords)}\n"
        output += "🔍 Try more specific keywords, check spelling, or use 'list drive files'.\n"
    elif total == 1:
        f = all_results[0]
        output += f"✅ Found 1 file: **{f['name']}**\n\n"
        if f["type"] == "local":
            output += "💡 Say: **open it** or **open file 1**\n"
        else:
            output += "💡 Say: **download it** or **download file 1**\n"
    else:
        output += f"📊 **Found {total} files!**\n\n"
        output += "❓ Say: **open file 2**, **download file 3**, etc.\n"

    return output


@mcp.tool()
def open_search_result(user_id, file_number) -> str:
    """Open a local file from the last smart search by its result number."""
    uid         = _to_user_id(user_id)
    file_number = _to_int(file_number, -1)

    if uid is None:
        return "❌ Invalid user_id — please log in again."

    cache_data = db.get_search_cache(uid, "last_search")
    if not cache_data:
        return "❌ No recent search results. Please search first."

    results = json.loads(cache_data)
    target  = next((r for r in results if r["number"] == file_number), None)
    if not target:
        return f"❌ File #{file_number} not found in last search results."
    if target["type"] != "local":
        return f"❌ File #{file_number} is not a local file. Use 'download file {file_number}' instead."

    try:
        fp = target["path"]
        if os.name == "nt":
            os.startfile(fp)
        else:
            subprocess.Popen(["open", fp])
        return f"✅ Opened: {target['name']}"
    except Exception as e:
        return f"❌ Error opening file: {str(e)}"


@mcp.tool()
def download_search_result(user_id, file_number) -> str:
    """Download an email attachment or Drive file from the last smart search by result number."""
    uid         = _to_user_id(user_id)
    file_number = _to_int(file_number, -1)

    if uid is None:
        return "❌ Invalid user_id — please log in again."

    cache_data = db.get_search_cache(uid, "last_search")
    if not cache_data:
        return "❌ No recent search results. Please search first."

    results = json.loads(cache_data)
    target  = next((r for r in results if r["number"] == file_number), None)
    if not target:
        return f"❌ File #{file_number} not found."
    if target["type"] == "local":
        return f"❌ File #{file_number} is already local at: {target['path']}"
    if target["type"] == "email":
        return download_attachment(
            user_id=uid,
            email_id=target["email_id"],
            filename=target["name"],
            attachment_id=target["attachment_id"],
        )
    if target["type"] == "drive":
        return download_drive_file(user_id=uid, file_id=target["file_id"], filename=target["name"])

    return f"❌ Unknown file type: {target['type']}"


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.initialize_database()
    print("✅ Database initialized")
    print("🚀 Starting MCP server on port 9006 ...")
    # NOTE: transport string must be "streamable-http" (hyphen, not underscore)
    mcp.run(transport="streamable-http")
