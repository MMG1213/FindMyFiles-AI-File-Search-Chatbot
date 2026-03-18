import streamlit as st
import asyncio
import threading
import subprocess
import os
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv()   # reads .env file so os.getenv() picks up GEMINI_API_KEY / GROQ_API_KEY

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

import database as db
import auth
import oauth_handler as oauth

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Smart Email & Drive Assistant",
    page_icon="📧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Bot system prompt ─────────────────────────────────────────────────────────
# NOTE: {user_id} is filled at chat time via .format(user_id=self.user_id)
BOT_PROMPT = """
You are an Intelligent Email & Drive Assistant with smart search capabilities.

**CRITICAL: Parameter Type Rules**
ALL parameters MUST be the correct type. DO NOT pass strings when integers or booleans are expected.

**CORRECT TYPE EXAMPLES:**
✅ fetch_emails(user_id={user_id}, max_results=10, unread_only=false)
✅ search_emails(user_id={user_id}, query="invoice", max_results=20)
✅ smart_search_with_memory(user_id={user_id}, query="photo", max_results=10)
✅ open_search_result(user_id={user_id}, file_number=3)

**Available Tools:**
- fetch_emails(user_id: int, max_results: int = 10, time_filter: str = "all", unread_only: bool = False)
- search_emails(user_id: int, query: str, max_results: int = 10, time_filter: str = "all")
- download_attachment(user_id: int, email_id: str, filename: str, attachment_id: str = None)
- list_drive_files(user_id: int, max_results: int = 10, query: str = None)
- search_drive_files(user_id: int, query: str, max_results: int = 20)
- download_drive_file(user_id: int, file_id: str, filename: str = None)
- get_drive_storage_info(user_id: int)
- smart_search_with_memory(user_id: int, query: str, max_results: int = 10)
- open_search_result(user_id: int, file_number: int)
- download_search_result(user_id: int, file_number: int)

**Current user_id: {user_id}** — always use this exact integer, never a string.

**Rules:**
1. For "files", "documents", or "photos" queries → use smart_search_with_memory first.
2. For general email queries ("show emails", "recent emails") → use fetch_emails.
3. For specific searches ("emails from John") → use search_emails.
4. When multiple results are found, list them with numbers and ask which the user wants.
5. When user says "file 1", "number 2", etc. → use open_search_result or download_search_result.
6. Only show files actually found — never fabricate results.

**File type hints:**
- "photo", "image", "picture" → jpg, png, gif, bmp, tiff
- "document" → pdf, docx, txt, doc
- "spreadsheet" → xlsx, csv, xls
"""


# ── Session state initialization ─────────────────────────────────────────────
def init_session_state():
    defaults = {
        "authenticated":  False,
        "user_id":        None,
        "session_token":  None,
        "user_info":      None,
        "messages":       [],
        "agent_manager":  None,
        "page":           "login",
        "last_file_count": 0,
        "oauth_success":  False,   # used on OAuth page to replace nested button
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ── SmartAgent ────────────────────────────────────────────────────────────────
class SmartAgent:
    """
    Wraps the LangGraph ReAct agent.

    Fix for Streamlit + asyncio conflict:
    We run the async initialisation and every ainvoke() call inside a
    dedicated background thread that owns its own event loop.  Streamlit
    itself may already have an event loop running in the main thread, so
    we must never call run_until_complete() on the main thread's loop.
    """

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.agent   = None
        self._lock   = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent")

    # ── internal async init ───────────────────────────────────────────────
    async def _init_agent(self):
        client = MultiServerMCPClient({
            "Email_Agent": {
                "url":       "http://localhost:9006/mcp",
                "transport": "streamable_http",   # langchain adapter uses underscore
            },
        })
        tools = await client.get_tools()

        if not tools:
            raise RuntimeError(
                "No tools loaded from MCP server. "
                "Make sure server.py is running:  python server.py"
            )

        print(f"🔧 Loaded {len(tools)} MCP tools: {[t.name for t in tools]}")

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY not set. "
                "Add it to your .env file:  GEMINI_API_KEY=your_key_here"
            )

        model = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            api_key=api_key,
            temperature=0,
        )
        return create_react_agent(model, tools)

    # ── run async code in the background thread ───────────────────────────
    def _run_in_thread(self, coro):
        """Submit a coroutine to the dedicated executor thread and block until done."""
        future = self._executor.submit(asyncio.run, coro)
        return future.result()   # blocks the Streamlit thread; raises on exception

    # ── lazy init ─────────────────────────────────────────────────────────
    def _ensure_initialized(self):
        with self._lock:
            if self.agent is None:
                self.agent = self._run_in_thread(self._init_agent())

    # ── public chat method ────────────────────────────────────────────────
    def chat(self, user_message: str, history: list) -> str:
        self._ensure_initialized()

        system_prompt = BOT_PROMPT.format(user_id=self.user_id)
        messages = [{"role": "system", "content": system_prompt}]
        # Limit history to last 20 messages to avoid exceeding context window
        for msg in history[-20:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})

        async def _invoke():
            return await self.agent.ainvoke({"messages": messages})

        try:
            response     = self._run_in_thread(_invoke())
            last_message = response["messages"][-1]
            content      = last_message.content if hasattr(last_message, "content") else last_message
            return self._extract_clean_text(content)
        except Exception as e:
            err = f"❌ Error: {str(e)}"
            if "Connection" in str(e) or "refused" in str(e):
                err += "\n\n🔌 Make sure the MCP server is running:  `python server.py`"
            elif "GEMINI_API_KEY" in str(e) or "api_key" in str(e).lower():
                err += "\n\n🔑 Check your GEMINI_API_KEY in the .env file."
            return err

    def _extract_clean_text(self, content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    parts.append(item["text"])
                elif hasattr(item, "text"):
                    parts.append(item.text)
                else:
                    parts.append(str(item))
            return "\n".join(filter(None, parts))
        return str(content)

    def reset(self):
        with self._lock:
            self.agent = None
        self._executor.shutdown(wait=False)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent")


# ── Page: Login / Register ────────────────────────────────────────────────────
def show_login_page():
    st.title("📧 Smart Email & Drive Assistant")
    st.markdown("---")

    _, col, _ = st.columns([1, 2, 1])
    with col:
        tab_login, tab_reg = st.tabs(["🔐 Login", "📝 Register"])

        # Login
        with tab_login:
            st.subheader("Welcome back!")
            with st.form("login_form"):
                username = st.text_input("Username", placeholder="Enter your username")
                password = st.text_input("Password", type="password", placeholder="Enter your password")
                submitted = st.form_submit_button("🔓 Login", use_container_width=True)

                if submitted:
                    if not username or not password:
                        st.error("❌ Please fill in all fields")
                    else:
                        with st.spinner("Logging in…"):
                            success, msg, user_id = auth.login_user(username, password)
                        if success:
                            session_token = auth.create_user_session(user_id)
                            st.session_state.authenticated  = True
                            st.session_state.user_id        = user_id
                            st.session_state.session_token  = session_token
                            st.session_state.user_info      = auth.get_user_info(user_id)
                            history = db.get_chat_history(user_id, limit=20)
                            st.session_state.messages = [
                                {"role": h["role"], "content": h["content"]} for h in history
                            ]
                            st.success(f"✅ {msg}")
                            st.rerun()
                        else:
                            st.error(f"❌ {msg}")

        # Register
        with tab_reg:
            st.subheader("Create Account")
            with st.form("register_form"):
                r_username = st.text_input("Username", placeholder="Choose a username")
                r_email    = st.text_input("Email",    placeholder="your.email@example.com")
                r_pass     = st.text_input("Password", type="password",
                                           placeholder="Min 8 chars, 1 uppercase, 1 number")
                r_pass2    = st.text_input("Confirm Password", type="password",
                                           placeholder="Re-enter password")
                submitted  = st.form_submit_button("📝 Register", use_container_width=True)

                if submitted:
                    if not all([r_username, r_email, r_pass, r_pass2]):
                        st.error("❌ Please fill in all fields")
                    elif r_pass != r_pass2:
                        st.error("❌ Passwords don't match")
                    else:
                        with st.spinner("Creating account…"):
                            success, msg, user_id = auth.register_user(r_username, r_email, r_pass)
                        if success:
                            st.success(f"✅ {msg}")
                            st.session_state.page         = "oauth_setup"
                            st.session_state.temp_user_id = user_id
                            st.rerun()
                        else:
                            st.error(f"❌ {msg}")

        st.markdown("---")
        st.caption("🔒 Your data is encrypted and secure")


# ── Page: OAuth setup ─────────────────────────────────────────────────────────
def show_oauth_setup_page():
    st.title("📧 Connect Your Gmail & Drive")
    st.markdown("---")

    _, col, _ = st.columns([1, 2, 1])
    with col:
        # If OAuth just succeeded, show success state and a go-to-login button
        if st.session_state.get("oauth_success"):
            st.success("✅ Gmail & Drive connected! You can now log in.")
            st.balloons()
            if st.button("➡️ Go to Login", use_container_width=True, type="primary"):
                st.session_state.page         = "login"
                st.session_state.oauth_success = False
                if "temp_user_id" in st.session_state:
                    del st.session_state["temp_user_id"]
                st.rerun()
            return

        st.info("""
        ### 🔗 Google Services Connection Required

        To use this app you need to connect your Google account.

        **What happens:**
        1. Click the button below
        2. A browser window will open
        3. Log in to your Google account
        4. Grant permissions for Gmail & Drive
        5. You'll be redirected back

        **We can access:**
        - 📧 Your emails (read, search, download attachments)
        - 📁 Your Drive files (read, search, download)

        **We never:**
        - Store your Google password
        - Share your data
        - Send emails without your consent
        """)

        if st.button("🔗 Connect Gmail & Drive", use_container_width=True, type="primary"):
            user_id = st.session_state.get("temp_user_id")
            if not user_id:
                st.error("❌ Session error — please register again.")
                st.rerun()
                return

            exists, msg = oauth.check_credentials_file()
            if not exists:
                st.error(msg)
            else:
                with st.spinner("Opening browser for Google authorization…"):
                    success, msg = oauth.initiate_oauth_flow(user_id)
                if success:
                    # Use session_state flag instead of nested button
                    st.session_state.oauth_success = True
                    st.rerun()
                else:
                    st.error(msg)

        if st.button("← Back to Login", use_container_width=True):
            st.session_state.page = "login"
            if "temp_user_id" in st.session_state:
                del st.session_state["temp_user_id"]
            st.rerun()


# ── Page: Main App ────────────────────────────────────────────────────────────
def show_main_app():
    user_id   = st.session_state.user_id        # int
    user_info = st.session_state.user_info      # dict from DB

    # Session guard
    if not auth.validate_user_session(st.session_state.session_token):
        st.error("⚠️ Session expired. Please log in again.")
        logout()
        return

    st.title("📧 Smart Email & Drive Assistant")
    # user_info has keys: id, username, email, is_gmail_connected, ...
    st.caption(f"Logged in as: **{user_info['username']}** ({user_info['email']})")

    # ── Sidebar ───────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### 👤 Profile")
        st.markdown(f"**{user_info['username']}**")
        st.caption(user_info["email"])

        if st.button("🚪 Logout", use_container_width=True, type="primary"):
            logout()

        st.divider()

        st.subheader("👀 About")
        st.markdown(
            "Hello! I'm your Smart Assistant. "
            "I can help you find files from your Emails, Google Drive, and local folders."
        )

        st.divider()

        st.subheader("🧠 Model")
        st.markdown("Using **Gemini 2.5 Flash** for processing.")

        st.divider()

        # Google services status
        st.subheader("🔗 Connected Services")
        gmail_connected, gmail_email = oauth.verify_gmail_connection(user_id)
        if gmail_connected:
            st.success(f"📧 Gmail: {gmail_email}")
        else:
            st.error("📧 Gmail: Disconnected")

        drive_connected, drive_info = oauth.verify_drive_connection(user_id)
        if drive_connected:
            st.success(f"📁 Drive: {drive_info}")
        else:
            st.error("📁 Drive: Disconnected")

        if not gmail_connected or not drive_connected:
            if st.button("🔄 Reconnect Services", use_container_width=True):
                with st.spinner("Reconnecting…"):
                    success, msg = oauth.initiate_oauth_flow(user_id)
                if success:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

        st.divider()

        # Downloaded files panel
        col_title, col_refresh = st.columns([3, 1])
        with col_title:
            st.subheader("📂 Downloaded Files")
        with col_refresh:
            if st.button("🔄", help="Refresh", key="refresh_btn", use_container_width=True):
                st.rerun()

        # Build the per-user attachments path
        base_dir        = os.path.dirname(os.path.abspath(__file__))
        attachments_dir = os.path.join(base_dir, "user_data", f"user_{user_id}", "Attachments")
        os.makedirs(attachments_dir, exist_ok=True)

        files = sorted(
            [f for f in os.listdir(attachments_dir) if os.path.isfile(os.path.join(attachments_dir, f))],
            reverse=True,
        )
        current_count = len(files)

        # Toast notifications when file count changes
        if current_count != st.session_state.last_file_count:
            if current_count > st.session_state.last_file_count:
                st.toast("✅ New file downloaded!", icon="📥")
            elif current_count < st.session_state.last_file_count:
                st.toast("🗑️ File deleted", icon="✅")
        st.session_state.last_file_count = current_count

        if files:
            st.metric("Total Files", current_count)
            with st.container(height=400):
                for idx, filename in enumerate(files, 1):
                    fp          = os.path.join(attachments_dir, filename)
                    file_size   = os.path.getsize(fp) / 1024
                    file_mod    = datetime.fromtimestamp(os.path.getmtime(fp))
                    ext         = os.path.splitext(filename)[1].lower()
                    icon_map    = {
                        ".pdf": "📕", ".doc": "📘", ".docx": "📘",
                        ".xls": "📗", ".xlsx": "📗", ".ppt": "📙", ".pptx": "📙",
                        ".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️",
                        ".zip": "📦", ".rar": "📦", ".txt": "📄",
                    }
                    icon        = icon_map.get(ext, "📎")
                    new_badge   = "🆕 " if (datetime.now() - file_mod).total_seconds() < 60 else ""

                    st.markdown(f"{new_badge}**{idx}. {icon} {filename}**")
                    st.caption(f"💾 {file_size:.1f} KB • {file_mod.strftime('%I:%M %p')}")

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("👁️ Open", key=f"open_{filename}", use_container_width=True):
                            try:
                                if os.name == "nt":
                                    os.startfile(fp)
                                else:
                                    subprocess.Popen(["open", fp])
                            except Exception as e:
                                st.error(f"❌ {e}")
                    with c2:
                        with open(fp, "rb") as f:
                            st.download_button(
                                label="💾 Save",
                                data=f.read(),
                                file_name=filename,
                                key=f"dl_{filename}",
                                use_container_width=True,
                            )
                    with st.expander("⚙️ More"):
                        st.code(fp, language=None)
                        if st.button("🗑️ Delete", key=f"del_{filename}", type="secondary"):
                            os.remove(fp)
                            st.rerun()
                    st.divider()
        else:
            st.caption("No downloaded files yet.")

        st.divider()

        # Controls
        st.subheader("⚙️ Controls")
        if st.button("🗑️ Clear Chat", use_container_width=True):
            db.clear_chat_history(user_id)   # user_id is int here — fixed
            st.session_state.messages = []
            st.rerun()

        if st.button("🔄 Reset Agent", use_container_width=True):
            if st.session_state.agent_manager:
                st.session_state.agent_manager.reset()
                st.session_state.agent_manager = None
            st.success("Agent reset!")
            st.rerun()

    # ── Main chat area ────────────────────────────────────────────────────
    st.markdown("---")

    for message in st.session_state.messages:
        avatar = "👤" if message["role"] == "user" else "🤖"
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])

    if prompt := st.chat_input("Ask me about your emails, attachments, or files…"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        db.save_chat_message(user_id, "user", prompt)

        with st.chat_message("user", avatar="👤"):
            st.markdown(prompt)

        with st.chat_message("assistant", avatar="🤖"):
            with st.spinner("🤔 Processing…"):
                agent    = get_or_create_agent(user_id)
                history  = st.session_state.messages[:-1]
                response = agent.chat(prompt, history)
                st.markdown(response)
                st.session_state.messages.append({"role": "assistant", "content": response})
                db.save_chat_message(user_id, "assistant", response)


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_or_create_agent(user_id: int) -> SmartAgent:
    if st.session_state.agent_manager is None:
        with st.spinner("🔧 Initializing Email Assistant…"):
            st.session_state.agent_manager = SmartAgent(user_id)
    return st.session_state.agent_manager


def logout():
    if st.session_state.session_token:
        auth.logout_user(st.session_state.session_token)
    for key in ("authenticated", "user_id", "session_token", "user_info",
                "messages", "agent_manager"):
        st.session_state[key] = None if key != "authenticated" else False
    st.session_state.messages      = []
    st.session_state.agent_manager = None
    st.session_state.page          = "login"
    st.rerun()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    db.initialize_database()
    init_session_state()
    db.cleanup_expired_sessions()

    if not st.session_state.authenticated:
        if st.session_state.page == "oauth_setup":
            show_oauth_setup_page()
        else:
            show_login_page()
    else:
        show_main_app()


if __name__ == "__main__":
    main()
