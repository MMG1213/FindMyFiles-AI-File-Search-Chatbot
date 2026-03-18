# Gmail-Drive-AI-File-Search-Chatbot

A conversational AI assistant that lets you search, browse, and download files from your Gmail, Google Drive, and local filesystem — all through a natural language chat interface powered by Gemini 2.5 Flash and LangGraph.

✨ Features

Conversational file search — ask for files in plain English across Gmail attachments, Google Drive, and local folders simultaneously
Email management — fetch, filter, and search emails with time-based filters (today, this week, last 7 days, etc.)
Attachment downloads — download email attachments directly from chat
Google Drive integration — list, search, and download Drive files; view storage quota
Smart search with memory — numbered results are cached so you can say "download file 3" in a follow-up message
Secure multi-user auth — bcrypt-hashed passwords, session tokens, and encrypted OAuth token storage
Per-user data isolation — each user's tokens, attachments, and chat history are fully isolated


🏗️ Architecture
┌─────────────────────────────┐
│ Streamlit Frontend (app.py) │  ← Chat UI, auth pages, file panel
└────────────┬────────────────┘
             │ LangGraph ReAct Agent
             │ (Gemini 2.5 Flash)
             ▼
┌─────────────────────────────┐
│ FastMCP Server (server.py)  │  ← port 9006, streamable-http
│ Tools: fetch_emails,        │
│ search_emails, smart_search │
│ download_attachment, etc.   │
└──────┬───────────┬──────────┘
       │           │
  Gmail API    Drive API
  (OAuth 2.0)  (OAuth 2.0)
       │           │
       └─────┬─────┘
             ▼
   SQLite DB (email_assistant.db)
   + Fernet-encrypted token store
The frontend and MCP server are two separate processes that must both be running at the same time.

🛠️ Tech Stack
LayerTechnologyUIStreamlitAI AgentLangGraph ReAct + Gemini 2.5 FlashTool ServerFastMCP (streamable-http, port 9006)LLM ProviderGoogle Generative AI (langchain-google-genai)Google APIsGmail API, Google Drive API (OAuth 2.0)DatabaseSQLite with WAL modeAuthbcrypt password hashing + session tokensEncryptionFernet symmetric encryption for OAuth tokens

⚙️ Prerequisites

Python 3.10+
A Google Cloud project with Gmail API and Drive API enabled
A Gemini API key from Google AI Studio


🚀 Setup
1. Clone the repository
bashgit clone https://github.com/your-username/smart-email-drive-assistant.git
cd smart-email-drive-assistant
2. Install dependencies
bashpip install -r requirements.txt
3. Configure environment variables
Copy the example env file and fill in your values:
bashcp _env .env
Edit .env:
envGEMINI_API_KEY=your_gemini_api_key_here
4. Add Google OAuth credentials

Go to Google Cloud Console → APIs & Services → Credentials
Create an OAuth 2.0 Client ID (Desktop app)
Enable the Gmail API and Drive API in your project
Download the credentials file and save it as credentials.json in the project root


The OAuth consent screen should request the following scopes:
gmail.readonly, gmail.modify, gmail.compose, drive.readonly, drive.file, drive.metadata.readonly

5. Initialize the database
bashpython database.py

▶️ Running the App
The app requires two terminals running simultaneously.
Terminal 1 — MCP Tool Server:
bashpython server.py
You should see:
✅ Database initialized
🚀 Starting MCP server on port 9006 ...
Terminal 2 — Streamlit Frontend:
bashstreamlit run app.py
Open your browser at http://localhost:8501.

🔐 First-time OAuth Setup

Register a new account in the app
You'll be prompted to connect Gmail & Drive via Google OAuth
A browser window will open — log in and grant the requested permissions
Once connected, your encrypted token is stored in the database and refreshed automatically

If your connection expires, use the "Reconnect Services" button in the sidebar.

💬 Example Queries
You sayWhat happensShow my unread emailsFetches unread Gmail messagesFind invoices from last weekSearches emails by keyword + time filterSearch for my resumeSearches local files, Gmail attachments, and DriveDownload file 2Downloads the second result from the last searchHow much Drive storage am I using?Returns storage quota detailsShow emails with attachments from JohnSearches Gmail with sender + attachment filter

📁 Project Structure
├── app.py               # Streamlit frontend + LangGraph agent
├── server.py            # FastMCP tool server (Gmail, Drive, local search)
├── auth.py              # Registration, login, session management
├── database.py          # SQLite schema + all DB operations
├── oauth_handler.py     # Google OAuth flow + credential management
├── encryption.py        # Fernet encryption for OAuth tokens
├── groq_app.py          # Alternative Groq-powered frontend
├── credentials.json     # Google OAuth client secrets (not committed)
├── encryption.key       # Fernet key (auto-generated, not committed)
├── requirements.txt
├── .env                 # Environment variables (not committed)
└── user_data/
    └── user_{id}/
        ├── token.json       # Cached OAuth token (per user)
        └── Attachments/     # Downloaded email attachments (per user)

🔒 Security Notes

Passwords are hashed with bcrypt (never stored in plain text)
Google OAuth tokens are Fernet-encrypted before being saved to the database
Session tokens are generated with secrets.token_urlsafe(32) and expire after 24 hours
credentials.json and encryption.key are listed in .gitignore — never commit these files


🗄️ Database Schema
TablePurposeusersAccounts, email, password hash, connection statususer_tokensEncrypted OAuth tokens per useruser_sessionsSession tokens with expirychat_historyLast 20 messages per user for LLM contextuser_downloadsDownload historysearch_cacheLast smart search results for follow-up commands

🧩 MCP Tools Reference
ToolDescriptionfetch_emailsFetch recent emails with optional time filter and unread flagsearch_emailsSearch emails by keyword and time windowdownload_attachmentDownload a specific email attachmentlist_drive_filesList files in Google Drivesearch_drive_filesSearch Drive by keyworddownload_drive_fileDownload a file from Google Driveget_drive_storage_infoShow Drive storage quotasmart_search_with_memorySearch local files, Gmail, and Drive simultaneouslyopen_search_resultOpen a local file from the last search by result numberdownload_search_resultDownload an email/Drive file from the last search by result number

🐛 Troubleshooting
No tools loaded from MCP server
→ Make sure server.py is running before starting the Streamlit app.
GEMINI_API_KEY not set
→ Check that your .env file exists and contains a valid key.
Gmail / Drive: Disconnected in sidebar
→ Click Reconnect Services and complete the OAuth flow again.
database is locked
→ The SQLite database uses WAL mode and a 5-second busy timeout. If it persists, make sure you don't have stale processes holding the DB open.

📄 License
MIT
