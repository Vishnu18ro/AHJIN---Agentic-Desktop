"""Conversational File Agent — Multi-turn file search, location clarification, and disambiguation."""

import re
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import ContextTypes

from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.file_search import FileSearchTool
from ahjin.tools.file_send import FileSendTool
from ahjin.tools.image_edit import ImageEditTool
from ahjin.tools.base import ToolInvocationRequest
from ahjin.beru.tool_planner import ToolIntentPlanner

if TYPE_CHECKING:
    from ahjin.harness.gateway import ProviderGateway
    from ahjin.tools.registry import ToolRegistry

logger = structlog.get_logger()

# Maximum session idle time before resetting state (5 minutes)
SESSION_TTL_SECONDS = 300.0


class FileSessionState(str, Enum):
    IDLE = "IDLE"
    AWAITING_LOCATION = "AWAITING_LOCATION"
    AWAITING_SELECTION = "AWAITING_SELECTION"


class FileSession(BaseModel):
    """Chat-scoped session tracking multi-turn file interaction state."""

    chat_id: int
    state: FileSessionState = FileSessionState.IDLE
    query: str = ""
    candidates: list[Path] = Field(default_factory=list)
    last_active: float = Field(default_factory=time.monotonic)
    original_instruction: str = ""
    image_operation: str | None = None
    target_kb: int | None = None

    def is_expired(self) -> bool:
        return (time.monotonic() - self.last_active) > SESSION_TTL_SECONDS

    def touch(self) -> None:
        self.last_active = time.monotonic()


class FileSessionManager:
    """Manages in-memory conversational sessions per Telegram chat."""

    def __init__(self) -> None:
        self._sessions: dict[int, FileSession] = {}

    def get_session(self, chat_id: int) -> FileSession:
        session = self._sessions.get(chat_id)
        if session is None or session.is_expired():
            session = FileSession(chat_id=chat_id)
            self._sessions[chat_id] = session
        else:
            session.touch()
        return session

    def reset_session(self, chat_id: int) -> None:
        if chat_id in self._sessions:
            self._sessions[chat_id] = FileSession(chat_id=chat_id)


def _format_file_size(size_bytes: int) -> str:
    """Format bytes into a clean, human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def _build_location_keyboard() -> InlineKeyboardMarkup:
    """Build Telegram inline buttons for search location selection."""
    keyboard = [
        [
            InlineKeyboardButton("🖥️ Desktop", callback_data="fileloc:desktop"),
            InlineKeyboardButton("📥 Downloads", callback_data="fileloc:downloads"),
        ],
        [
            InlineKeyboardButton("📄 Documents", callback_data="fileloc:documents"),
            InlineKeyboardButton("🌐 Search Everywhere", callback_data="fileloc:pc"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="fileloc:cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_selection_keyboard(candidates: list[Path]) -> InlineKeyboardMarkup:
    """Build Telegram inline buttons for multiple file candidates."""
    keyboard: list[list[InlineKeyboardButton]] = []
    for idx, cand in enumerate(candidates):
        button_text = f"{idx + 1}. {cand.name}"
        if len(button_text) > 40:
            button_text = button_text[:37] + "..."
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"filesel:{idx}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="filesel:cancel")])
    return InlineKeyboardMarkup(keyboard)


class FileAgent:
    """Conversational Agent for file search, location clarification, and delivery.

    Features:
    1. Detects file search / delivery intent in user messages.
    2. Proactively prompts for location (Desktop, Downloads, Documents, Everywhere) via inline buttons or text.
    3. Handles multi-file disambiguation when multiple matches exist (e.g. nikhil_resume vs yashuresume).
    4. Delivers the chosen file directly via Telegram document attachments.
    """

    def __init__(
        self,
        search_tool: FileSearchTool | None = None,
        send_tool: FileSendTool | None = None,
        image_edit_tool: ImageEditTool | None = None,
        path_policy: SafePathPolicy | None = None,
        gateway: "ProviderGateway | None" = None,
        tool_planner: ToolIntentPlanner | None = None,
    ) -> None:
        self.path_policy = path_policy or SafePathPolicy()
        self.search_tool = search_tool or FileSearchTool(path_policy=self.path_policy)
        self.send_tool = send_tool or FileSendTool(path_policy=self.path_policy)
        self.image_edit_tool = image_edit_tool or ImageEditTool()
        self.gateway = gateway
        self.tool_planner = tool_planner or ToolIntentPlanner()
        self.session_manager = FileSessionManager()

    def detect_file_intent(self, text: str) -> tuple[bool, str, str | None]:
        """Detect if text is a file retrieval/search request.

        Returns:
            (is_file_request, extracted_query, extracted_location_or_none)
        """
        clean = text.strip()
        lower = clean.lower()

        # Cancellation triggers
        if lower in ("cancel", "stop", "nevermind", "abort", "exit"):
            return False, "", None

        # Exclude questions that are clearly not asking for files
        if any(lower.startswith(prefix) for prefix in ("how do i", "explain", "what is", "why is", "who is")):
            if not any(k in lower for k in ("my resume", "my file", "send file", "find file")):
                return False, "", None

        # Location keyword check
        detected_location: str | None = None
        for loc in ("downloads", "desktop", "documents", "workspace"):
            if f"in {loc}" in lower or f"from {loc}" in lower or f"on {loc}" in lower:
                detected_location = loc
                break

        # Patterns for file requests
        # e.g., "send me my resume", "can you send me the invoice.pdf", "find resume", "get document"
        patterns = [
            r"^(?:please\s+)?(?:can you\s+)?(?:send|give|get|attach|share|download|fetch)\s+(?:me\s+)?(?:the\s+|my\s+|a\s+)?([a-zA-Z0-9_\-\.\s]+?)(?:\s+(?:from|in|on)\s+(?:my\s+)?(?:desktop|downloads|documents|workspace|pc))?$",
            r"^(?:please\s+)?(?:find|search(?:\s+for)?|locate|where is)\s+(?:my\s+|the\s+|a\s+)?([a-zA-Z0-9_\-\.\s]+?)(?:\s+(?:from|in|on)\s+(?:my\s+)?(?:desktop|downloads|documents|workspace|pc))?$",
        ]

        for pat in patterns:
            m = re.match(pat, clean, re.IGNORECASE)
            if m:
                raw_query = m.group(1).strip()
                # Clean filler words
                raw_query = re.sub(r"\b(file|document|doc|pdf|copy)\b", "", raw_query, flags=re.IGNORECASE).strip()
                if raw_query and len(raw_query) >= 2:
                    return True, raw_query, detected_location

        # Simple keyword fallback: e.g. "resume", "send resume"
        send_verbs = ("send", "give me", "find", "search", "locate", "share", "attach", "get")
        file_nouns = ("resume", "cv", "invoice", "receipt", "document", "notes", "paper", "report")
        for noun in file_nouns:
            if noun in lower:
                if any(verb in lower for verb in send_verbs) or lower.startswith(noun):
                    # Extract query around noun
                    return True, noun, detected_location

        return False, "", None

    def resolve_location_text(self, text: str) -> str:
        """Resolve natural language response to target search root."""
        lower = text.strip().lower()

        # Global / everywhere indicators
        if any(k in lower for k in ("everywhere", "every where", "all", "whole", "anywhere", "pc", "dont know", "don't know", "idk", "entire")):
            return "pc"

        if "download" in lower:
            return "downloads"
        if "desktop" in lower:
            return "desktop"
        if "document" in lower:
            return "documents"
        if "workspace" in lower:
            return "workspace"

        # Default fallback to PC-wide search if uncertain
        return "pc"

    def resolve_selection_text(self, text: str, candidates: list[Path]) -> int | None:
        """Resolve user response to a candidate index."""
        clean = text.strip().lower()

        # 1. Number lookup: "1", "2", "#1", "first", "second"
        digit_m = re.search(r"\b([1-9])\b", clean)
        if digit_m:
            idx = int(digit_m.group(1)) - 1
            if 0 <= idx < len(candidates):
                return idx

        ordinal_map = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "last": len(candidates) - 1}
        for ord_word, idx in ordinal_map.items():
            if ord_word in clean and 0 <= idx < len(candidates):
                return idx

        # 2. Filename substring match
        clean_no_ext = re.sub(r"\.[a-zA-Z0-9]+$", "", clean)
        for idx, cand in enumerate(candidates):
            cand_name_lower = cand.name.lower()
            cand_stem_lower = cand.stem.lower()
            if clean in cand_name_lower or clean_no_ext in cand_stem_lower:
                return idx
            # Match if parts of clean are in cand name
            words = [w for w in clean_no_ext.split() if len(w) > 2]
            if words and all(w in cand_stem_lower for w in words):
                return idx

        return None

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle incoming text message.

        Returns:
            True if this message was intercepted and handled by the FileAgent.
            False if it should fall through to the standard dispatcher.
        """
        if not update.message or not update.message.text:
            return False

        chat_id = update.message.chat_id
        text = update.message.text.strip()
        session = self.session_manager.get_session(chat_id)

        # 1. Check if user wants to cancel an active file conversation
        if session.state != FileSessionState.IDLE and text.lower() in ("cancel", "stop", "nevermind", "abort", "exit"):
            self.session_manager.reset_session(chat_id)
            await update.message.reply_text("❌ File search cancelled.")
            return True

        # 2. State: AWAITING_LOCATION
        if session.state == FileSessionState.AWAITING_LOCATION:
            resolved_loc = self.resolve_location_text(text)
            await self._execute_search_and_respond(update, context, session, resolved_loc)
            return True

        # 3. State: AWAITING_SELECTION
        if session.state == FileSessionState.AWAITING_SELECTION:
            selected_idx = self.resolve_selection_text(text, session.candidates)
            if selected_idx is not None:
                chosen_file = session.candidates[selected_idx]
                await self._deliver_file(update, context, session, chosen_file)
                return True
            else:
                # Clarify choice
                await update.message.reply_text(
                    f"I couldn't identify which file you meant by '{text}'. "
                    f"Please reply with a number (1-{len(session.candidates)}) or tap a button above.",
                    reply_markup=_build_selection_keyboard(session.candidates),
                )
                return True

        # 4. State: IDLE — Check if this is a new file request
        intent = await self.tool_planner.plan_tool_intent(text)
        if not intent:
            return False

        detected_loc = None
        query = ""

        if intent.tool_name == "file_search":
            query = str(intent.parameters.get("query", ""))
            detected_loc = str(intent.parameters.get("path", ""))
        elif intent.tool_name == "image_edit":
            path_str = str(intent.parameters.get("path", ""))
            query = Path(path_str).name if path_str else ""
            detected_loc = str(Path(path_str).parent) if path_str and "/" in path_str else None
            session.image_operation = str(intent.parameters.get("operation", ""))
            try:
                session.target_kb = int(intent.parameters.get("target_kb", 0))
            except ValueError:
                pass
        else:
            return False

        if not query:
            return False

        session.query = query
        session.original_instruction = text

        # If location is already specified (e.g. "send me resume from downloads")
        if detected_loc:
            await self._execute_search_and_respond(update, context, session, detected_loc)
            return True

        # Ask user where to search
        session.state = FileSessionState.AWAITING_LOCATION
        reply_text = (
            f"📁 Where should I search for **\"{query}\"**?\n\n"
            "Tap an option below or type your answer (e.g. *\"downloads\"* or *\"search everywhere\"*):"
        )
        await update.message.reply_text(
            reply_text,
            reply_markup=_build_location_keyboard(),
            parse_mode="Markdown",
        )
        return True

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle inline keyboard button callbacks."""
        query = update.callback_query
        if not query or not query.data:
            return False

        await query.answer()
        chat_id = query.message.chat_id if query.message else 0
        data = query.data
        session = self.session_manager.get_session(chat_id)

        # Handle location buttons: fileloc:<root>
        if data.startswith("fileloc:"):
            loc_val = data.split(":", 1)[1]
            if loc_val == "cancel":
                self.session_manager.reset_session(chat_id)
                if query.message:
                    await query.message.edit_text("❌ File search cancelled.")
                return True

            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
            await self._execute_search_and_respond(update, context, session, loc_val)
            return True

        # Handle candidate selection buttons: filesel:<idx>
        if data.startswith("filesel:"):
            sel_val = data.split(":", 1)[1]
            if sel_val == "cancel":
                self.session_manager.reset_session(chat_id)
                if query.message:
                    await query.message.edit_text("❌ File selection cancelled.")
                return True

            try:
                idx = int(sel_val)
                if 0 <= idx < len(session.candidates):
                    chosen_file = session.candidates[idx]
                    if query.message:
                        await query.message.edit_reply_markup(reply_markup=None)
                    await self._deliver_file(update, context, session, chosen_file)
                    return True
            except (ValueError, IndexError):
                pass

        return False

    async def _execute_search_and_respond(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session: FileSession,
        location: str,
    ) -> None:
        """Execute file search and branch response based on match count."""
        target_msg = update.message or (update.callback_query.message if update.callback_query else None)
        if target_msg is None:
            return

        loc_label = "Everywhere (PC)" if location == "pc" else location.title()
        status_msg = await target_msg.reply_text(f"🔍 Searching for **\"{session.query}\"** in **{loc_label}**...")

        candidates = self.search_tool.find_matching_files(session.query, path_str=location, max_results=10)
        session.candidates = candidates

        # Branch 1: Zero matches
        if not candidates:
            session.state = FileSessionState.IDLE
            if location != "pc":
                retry_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Search Everywhere (Entire PC)", callback_data="fileloc:pc")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="fileloc:cancel")],
                ])
                session.state = FileSessionState.AWAITING_LOCATION
                await status_msg.edit_text(
                    f"⚠️ No files matching **\"{session.query}\"** found in **{loc_label}**.\n\n"
                    "Would you like me to search across your entire PC?",
                    reply_markup=retry_keyboard,
                    parse_mode="Markdown",
                )
            else:
                await status_msg.edit_text(
                    f"⚠️ No files matching **\"{session.query}\"** were found on your computer.",
                    parse_mode="Markdown",
                )
            return

        # Branch 2: Exactly 1 match
        if len(candidates) == 1:
            candidate = candidates[0]
            await status_msg.edit_text(f"📄 Found **{candidate.name}**! Sending it now...")
            await self._deliver_file(update, context, session, candidate)
            return

        # Branch 3: Multiple matches (e.g. nikhil_resume vs yashuresume)
        session.state = FileSessionState.AWAITING_SELECTION
        lines: list[str] = [
            f"🔍 I found **{len(candidates)} files** matching **\"{session.query}\"**:\n"
        ]
        for idx, cand in enumerate(candidates, start=1):
            parent_folder = cand.parent.name or "Root"
            size_str = _format_file_size(cand.stat().st_size) if cand.exists() else ""
            lines.append(f"{idx}. 📄 **`{cand.name}`** _({parent_folder} • {size_str})_")

        lines.append("\n**Which one would you like me to send?**\nTap a button below or reply with the number:")

        await status_msg.edit_text(
            "\n".join(lines),
            reply_markup=_build_selection_keyboard(candidates),
            parse_mode="Markdown",
        )

    async def _deliver_file(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session: FileSession,
        file_path: Path,
    ) -> None:
        """Validate safety, attach, and deliver file via Telegram document upload."""
        target_msg = update.message or (update.callback_query.message if update.callback_query else None)
        if target_msg is None:
            return

        if not file_path.exists() or not file_path.is_file():
            await target_msg.reply_text(f"❌ Error: File '{file_path.name}' no longer exists on disk.")
            self.session_manager.reset_session(session.chat_id)
            return

        # Check sensitivity
        if self.path_policy.is_sensitive_file(file_path):
            await target_msg.reply_text(f"🔒 Security Notice: Access to sensitive file '{file_path.name}' is prohibited.")
            self.session_manager.reset_session(session.chat_id)
            return

        # Check file size (max 50 MB for Telegram bots)
        size_bytes = file_path.stat().st_size
        if size_bytes > 50 * 1024 * 1024:
            await target_msg.reply_text(
                f"⚠️ File '{file_path.name}' ({_format_file_size(size_bytes)}) exceeds "
                "Telegram's 50 MB attachment limit."
            )
            self.session_manager.reset_session(session.chat_id)
            return

        # Perform image edit if requested
        if session.image_operation and session.image_operation in ("compress", "enlarge", "resize"):
            status_msg = await target_msg.reply_text(f"🖼️ Performing **{session.image_operation}** on {file_path.name}...")
            
            edit_req = ToolInvocationRequest(
                tool_name="image_edit",
                parameters={
                    "path": str(file_path.resolve()),
                    "operation": session.image_operation,
                    "target_kb": session.target_kb,
                }
            )
            
            result = await self.image_edit_tool.execute(edit_req)
            if not result.success:
                await status_msg.edit_text(f"❌ Failed to {session.image_operation} image: {result.error.message if result.error else 'Unknown error'}")
                self.session_manager.reset_session(session.chat_id)
                return
            
            # Use the newly created image path
            if result.output and isinstance(result.output, dict) and "attachment_paths" in result.output and result.output["attachment_paths"]:
                file_path = Path(result.output["attachment_paths"][0])
                size_bytes = file_path.stat().st_size
                await status_msg.edit_text(f"✅ Image processed successfully! New size: {_format_file_size(size_bytes)}")
            else:
                await status_msg.edit_text("❌ Image processing failed to return output path.")
                self.session_manager.reset_session(session.chat_id)
                return

        # Upload document
        try:
            with file_path.open("rb") as doc_file:
                await target_msg.reply_document(
                    document=doc_file,
                    filename=file_path.name,
                    caption=f"📄 {file_path.name} ({_format_file_size(size_bytes)})",
                    read_timeout=120.0,
                    write_timeout=120.0,
                    connect_timeout=30.0,
                )
            logger.info("File delivered successfully via Telegram", file_name=file_path.name, chat_id=session.chat_id)
        except TimedOut:
            logger.warning(
                "Upload timed out locally waiting for Telegram response; Telegram may still deliver",
                file_name=file_path.name,
                chat_id=session.chat_id,
            )
            # Avoid showing a false failure error since Telegram's servers often complete delivery
        except Exception as exc:
            logger.error("Failed to send document in Telegram", error=str(exc), file_name=file_path.name)
            await target_msg.reply_text(f"❌ Failed to send document: {exc}")

        # Reset session to IDLE after delivery
        self.session_manager.reset_session(session.chat_id)
