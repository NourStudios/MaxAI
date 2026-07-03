import os
import cv2
import time
import json
import re
import sys
import torch
import numpy as np
import pyautogui
import subprocess
import win32gui
import win32con
from PIL import Image
import ollama
import requests
import tkinter as tk
import ctypes

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _safe_json_loads(json_str):
    """Parse JSON safely. Returns (parsed, error_msg)."""
    try:
        return json.loads(json_str), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _is_task_step(step):
    """Check if a decomposition step looks like a real automation task, not conversational text."""
    if not isinstance(step, str):
        return False
    step_lower = step.lower().strip()
    if not step_lower:
        return False
    
    conversational_markers = [
        "okay", "ok", "sure", "will do", "i'll do", "i will do",
        "task", "done", "completed", "reply", "response", "here is",
        "here are", "steps", "explain", "explaining", "chat", "message",
        "here's", "certainly", "absolutely", "currently", "i did"
    ]
    
    words = step_lower.split()
    if not words:
        return False
    
    action_words = [
        'launch', 'open', 'close', 'click', 'type', 'search', 'find',
        'go', 'make', 'create', 'build', 'run', 'execute', 'start',
        'stop', 'wait', 'focus', 'save', 'delete', 'write', 'read',
        'scroll', 'press', 'hotkey', 'navigate', 'select', 'switch',
        'install', 'download', 'watch', 'play'
    ]
    
    has_action = any(w in action_words for w in words)
    has_conversational = any(p in step_lower for p in conversational_markers)
    
    if has_conversational and not has_action:
        return False
    
    return True


def _is_valid_command(step):
    """Check if a string looks like a valid automation command step."""
    if not isinstance(step, str):
        return False
    step_lower = step.lower().strip()
    if not step_lower:
        return False
    
    command_prefixes = {
        'press', 'hotkey', 'type', 'wait', 'waiting', 'type_in', 'launch',
        'click', 'double_click', 'right_click', 'move_to', 'move_rel', 'scroll',
        'tab', 'find_text', 'find_image', 'create_file', 'write_file', 'read_file',
        'delete_file', 'run_file', 'list_files', 'set_workspace', 'self_correct',
        'browser_action', 'screen_analyze', 'chat_reply', 'focus_app',
        'select_profile', 'vscode_action', 'antigravity_action', 'create_project',
        'write_in_code', 'open_file_explorer', 'expect', 'create'
    }
    
    words = step_lower.split()
    if not words:
        return False
    
    first_word = words[0]
    return first_word in command_prefixes


def _merge_split_commands(plan):
    """Merge standalone command words with following text arguments if they got split across array elements."""
    if not plan or not isinstance(plan, list):
        return plan
    
    merged = []
    i = 0
    while i < len(plan):
        item = plan[i]
        if not isinstance(item, str):
            merged.append(item)
            i += 1
            continue
        
        item_lower = item.lower().strip()
        words = item_lower.split()
        
        # Check if this is a standalone command word that needs an argument
        if item_lower in {"type", "press", "hotkey", "wait", "type_in", "find_text", "find_image",
                          "click", "double_click", "right_click", "move_to", "move_rel", "scroll",
                          "tab", "create_file", "write_file", "read_file", "delete_file", "run_file",
                          "list_files", "set_workspace", "focus_app", "browser_action",
                          "vscode_action", "antigravity_action", "create_project", "write_in_code",
                          "open_file_explorer", "expect", "create"}:
            # Look ahead for the next string element to use as argument
            if i + 1 < len(plan) and isinstance(plan[i + 1], str):
                next_item = plan[i + 1]
                next_lower = next_item.lower().strip()
                # Only merge if the next item is NOT itself a command
                next_words = next_lower.split()
                if next_words and next_words[0] not in {
                    'press', 'hotkey', 'type', 'wait', 'waiting', 'type_in', 'launch',
                    'click', 'double_click', 'right_click', 'move_to', 'move_rel', 'scroll',
                    'tab', 'find_text', 'find_image', 'create_file', 'write_file', 'read_file',
                    'delete_file', 'run_file', 'list_files', 'set_workspace', 'self_correct',
                    'browser_action', 'screen_analyze', 'chat_reply', 'focus_app',
                    'select_profile', 'vscode_action', 'antigravity_action', 'create_project',
                    'write_in_code', 'open_file_explorer', 'expect', 'create'
                }:
                    merged.append(f"{item} {next_item}")
                    i += 2
                    continue
        
        merged.append(item)
        i += 1
    
    return merged


def _clean_plan(plan, original_prompt):
    """Post-process a decomposed plan to fix common LLM mistakes."""
    plan = _merge_split_commands(plan)
    if not plan or not isinstance(plan, list):
        return plan
    
    prompt_lower = original_prompt.lower().strip()
    cleaned = []
    has_click_step = False
    has_type_search = False
    has_navigate_to_site = False
    
    # First pass: clean TYPE arguments and detect missing steps
    for step in plan:
        if not isinstance(step, str):
            cleaned.append(step)
            continue
        
        step_lower = step.lower().strip()
        
        # Clean TYPE arguments
        if step_lower.startswith("type "):
            text = step_lower.split(" ", 1)[1].strip().strip("'\"")
            # Remove "search" prefix if it's at the start
            if text.startswith("search "):
                text = text[len("search "):].strip()
            # Remove "for" prefix
            if text.startswith("for "):
                text = text[len("for "):].strip()
            # Remove "online" suffix
            text = text.replace(" online", "").replace(" on the internet", "").strip()
            # Remove trailing action phrases
            for phrase in [" and go to", " and click", " and open", " and visit", " then go to", " then click"]:
                if phrase in text:
                    text = text.split(phrase)[0].strip()
            
            if text:
                cleaned.append(f"TYPE {text}")
            else:
                cleaned.append(step)  # Keep original if we can't clean it
            
            # Check if this is a search query
            if "search" in text or "find" in text:
                has_type_search = True
        
        elif step_lower.startswith("click "):
            has_click_step = True
            cleaned.append(step)
        
        elif step_lower in ["go to youtube.com", "open youtube", "go to youtube"]:
            has_navigate_to_site = True
            cleaned.append(step)
        
        else:
            cleaned.append(step)
    
    # Second pass: add missing steps based on original prompt
    # Only add "click first search result" for actual web search tasks
    search_click_keywords = ["search", "find", "google", "youtube", "website", "site", "online"]
    is_web_search_task = any(kw in prompt_lower for kw in search_click_keywords)
    has_web_navigation = any(s.lower().strip() in ["go to youtube.com", "open youtube", "go to youtube"] or "google.com" in s.lower() or "youtube.com" in s.lower() for s in cleaned if isinstance(s, str))
    
    if is_web_search_task and not has_click_step and not has_web_navigation:
        if not any(s.lower().strip().startswith("click ") for s in cleaned if isinstance(s, str)):
            if len(cleaned) > 0:
                cleaned.append("click first search result")
    
    # Do NOT auto-inject google.com navigation for search tasks.
    # The decomposition should already include the correct search/navigation steps.
    # Auto-injecting google.com caused "search in Google" behavior instead of direct address-bar search.
    
    # Safety net: if a TYPE step contains a URL but is not followed by PRESS enter, add it
    for i, step in enumerate(cleaned):
        if isinstance(step, str) and step_lower.startswith("type "):
            text = step_lower.split(" ", 1)[1].strip().strip("'\"")
            if text.startswith("http://") or text.startswith("https://") or text.startswith("www.") or "." in text:
                # Looks like a URL - ensure next step is PRESS enter
                if i + 1 >= len(cleaned) or not (isinstance(cleaned[i + 1], str) and cleaned[i + 1].lower().strip() == "press enter"):
                    cleaned.insert(i + 1, "PRESS enter")
                break
    
    # Deduplicate consecutive identical steps
    deduped = []
    for step in cleaned:
        if not deduped or not (isinstance(step, str) and isinstance(deduped[-1], str) and step.lower().strip() == deduped[-1].lower().strip()):
            deduped.append(step)
    cleaned = deduped
    
    return cleaned


class ChatReply(Exception):
    """Raised when a user prompt is determined to be a chat question, not an automation task."""
    def __init__(self, reply_text):
        self.reply_text = reply_text
        super().__init__(reply_text)

# ==========================================
# WINDOWS DEPENDENCY COMPILER BYPASS PATCH
# ==========================================
import sys
from transformers.dynamic_module_utils import get_imports

sys.modules["flash_attn"] = None
os.environ["FLASH_ATTN_DISABLED"] = "1"
transformers_get_imports = get_imports

def fixed_get_imports(filename: str | os.PathLike) -> list[str]:
    imports = transformers_get_imports(filename)
    if "flash_attn" in imports:
        imports.remove("flash_attn")
    return imports

import transformers.dynamic_module_utils
transformers.dynamic_module_utils.get_imports = fixed_get_imports
from transformers import AutoProcessor, AutoModelForCausalLM

# ==========================================
# LOCAL INITIALIZATION
# ==========================================
print("[*] Loading Local Vision Engine (Florence-2)...")
device = "cuda" if torch.cuda.is_available() else "cpu"
torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32

model_id = "microsoft/Florence-2-base"
local_vision_model = AutoModelForCausalLM.from_pretrained(
    model_id, trust_remote_code=True, torch_dtype=torch_dtype, attn_implementation="sdpa"
).to(device)
local_vision_processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
print(f"[+] Local Vision active on: {device.upper()}")

_conversation_history = []
_max_history = 50
_conversation_file = os.path.join(os.getcwd(), "conversation_history.json")
_conversations_dir = os.path.join(os.getcwd(), "conversations")
_pending_verification = None
_current_chat_id = "default"


def _set_pending_verification(task):
    global _pending_verification
    _pending_verification = task


def _get_and_clear_pending_verification():
    global _pending_verification
    task = _pending_verification
    _pending_verification = None
    return task


def _ensure_conversations_dir():
    if not os.path.exists(_conversations_dir):
        os.makedirs(_conversations_dir, exist_ok=True)


def _get_chat_path(chat_id):
    _ensure_conversations_dir()
    return os.path.join(_conversations_dir, f"{chat_id}.json")


def _list_chat_sessions():
    _ensure_conversations_dir()
    sessions = []
    for name in os.listdir(_conversations_dir):
        if name.endswith(".json"):
            chat_id = name[:-5]
            path = os.path.join(_conversations_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                title = data.get("title") or chat_id
                updated = data.get("updated_at") or data.get("timestamp") or os.path.getmtime(path)
                sessions.append({
                    "id": chat_id,
                    "title": title,
                    "updated_at": updated,
                    "path": path,
                })
            except Exception:
                continue
    sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return sessions


def _load_conversation_history(chat_id=None):
    global _conversation_history, _current_chat_id
    _current_chat_id = chat_id or _current_chat_id or "default"
    path = _get_chat_path(_current_chat_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _conversation_history = data.get("messages", [])[-_max_history:]
            elif isinstance(data, list):
                _conversation_history = data[-_max_history:]
    except Exception:
        pass


def _save_conversation_history(title=None):
    global _conversation_history, _current_chat_id
    _ensure_conversations_dir()
    path = _get_chat_path(_current_chat_id)
    try:
        payload = {
            "id": _current_chat_id,
            "title": title or _current_chat_id,
            "updated_at": time.time(),
            "messages": _conversation_history[-_max_history:],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _add_conversation(role, text, meta=None):
    entry = {
        "role": role,
        "text": text,
        "timestamp": time.time(),
        "meta": meta or {}
    }
    _conversation_history.append(entry)
    if len(_conversation_history) > _max_history:
        _conversation_history.pop(0)
    _save_conversation_history()

def _get_conversation_summary():
    if not _conversation_history:
        return "No prior context."
    lines = []
    for item in _conversation_history[-6:]:
        meta = item.get("meta", {})
        window = meta.get("window", "")
        screen = meta.get("screen_summary", "")
        parts = [f"{item['role']}: {item['text'][:120]}"]
        if window:
            parts.append(f" [window: {window}]")
        if screen:
            parts.append(f" [screen: {screen[:100]}]")
        lines.append("".join(parts))
    return "\n".join(lines)

def _get_screen_context_explanation(win_name, is_ide, is_browser, ide_type=""):
    parts = []
    if win_name and win_name != "(no active window)":
        parts.append(f"Active window: {win_name}")
    else:
        parts.append("No active window detected")
    if is_ide:
        parts.append(f"IDE detected: {ide_type or 'unknown'}")
    if is_browser:
        parts.append("Browser window active")
    return "; ".join(parts) if parts else "Unknown screen state"

_load_conversation_history()
print(f"[💾 HISTORY] Loaded {len(_conversation_history)} conversation entries from disk")

_skip_waits = False

def set_skip_waits(value):
    """Set whether to skip WAIT commands."""
    global _skip_waits
    _skip_waits = value

def get_skip_waits():
    """Get whether to skip WAIT commands."""
    return _skip_waits

def reason_overlay(text):
    """Display reasoning in a top-left overlay window. Updates without stealing focus."""
    try:
        import tkinter as tk
        
        overlay_exists = getattr(reason_overlay, "_overlay", None)
        if overlay_exists:
            try:
                reason_overlay._label.config(text=text[:600], bg="#1e1e1e", fg="#ffffff")
                reason_overlay._overlay.deiconify()
            except Exception:
                pass
            return
        
        reason_overlay._overlay = tk.Tk()
        reason_overlay._overlay.overrideredirect(True)
        reason_overlay._overlay.attributes("-topmost", True)
        reason_overlay._overlay.configure(bg="#1e1e1e")
        reason_overlay._overlay.geometry("500x180+20+20")
        
        reason_overlay._frame = tk.Frame(reason_overlay._overlay, bg="#1e1e1e", padx=12, pady=10)
        reason_overlay._frame.pack(fill="both", expand=True)
        
        title = tk.Label(reason_overlay._frame, text="AI REASONING", bg="#1e1e1e", fg="#00ff88", 
                        font=("Consolas", 13, "bold"))
        title.pack(anchor="w")
        
        reason_overlay._label = tk.Label(reason_overlay._frame, text=text[:500], bg="#1e1e1e", fg="#ffffff",
                                      font=("Consolas", 10), justify="left", wraplength=480)
        reason_overlay._label.pack(anchor="w", pady=3)
        
        def close_overlay(event=None):
            if getattr(reason_overlay, "_overlay", None):
                reason_overlay._overlay.destroy()
                reason_overlay._overlay = None
        
        reason_overlay._overlay.bind("<Escape>", close_overlay)
        reason_overlay._overlay.protocol("WM_DELETE_WINDOW", close_overlay)
        
        # Non-blocking update loop using after_idle
        def update_loop():
            if getattr(reason_overlay, "_overlay", None):
                try:
                    reason_overlay._overlay.update_idletasks()
                except:
                    pass
                reason_overlay._overlay.after(1000, update_loop)
        
        reason_overlay._overlay.after(100, update_loop)
        
    except Exception as e:
        print(f"[🔍 REASON] Overlay error: {e}")

# ==========================================
# INVISIBLE UNFOCUSABLE STATUS WINDOW
# ==========================================

class InvisibleStatusWindow:
    """Small unfocusable always-on-top status indicator that shows the current sub-task."""
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-disabled", True)
        self.root.configure(bg="#1e1e1e")
        
        self.label = tk.Label(self.root, text="", font=("Segoe UI", 10, "bold"),
                             bg="#1e1e1e", fg="#00ff88", justify="left", wraplength=380,
                             padx=8, pady=4)
        self.label.pack()
        
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"400x40+{screen_w-420}+{screen_h-60}")
        
        self._make_unfocusable()
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()
        
    def _make_unfocusable(self):
        try:
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TOPMOST = 0x00000008
            
            hwnd = self.root.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass
             
    def set_text(self, text):
        try:
            self.label.config(text=str(text)[:200])
            self.root.deiconify()
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass
            
    def destroy(self):
        try:
            self.root.destroy()
        except Exception:
            pass

_status_win = None

def set_status_text(text):
    global _status_win
    try:
        if _status_win is None:
            _status_win = InvisibleStatusWindow()
        _status_win.set_text(text)
    except Exception as e:
        print(f"[STATUS] {text}")

def hide_status_window():
    global _status_win
    if _status_win:
        try:
            _status_win.destroy()
        except Exception:
            pass
        _status_win = None



def vision_ocr(screenshot):
    """OCR using local Florence-2 model."""
    sw, sh = screenshot.size
    blocks = []
    try:
        task_prompt = "<OCR_WITH_REGION>"
        inputs = local_vision_processor(text=task_prompt, images=screenshot, return_tensors="pt").to(device, torch_dtype)
        with torch.no_grad():
            ids = local_vision_model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=1024, num_beams=3
            )
        raw = local_vision_processor.batch_decode(ids, skip_special_tokens=False)[0]
        parsed = local_vision_processor.post_process_generation(raw, task=task_prompt, image_size=screenshot.size)
        ocr_data = parsed.get(task_prompt, {})
        if ocr_data and "labels" in ocr_data and "quad_boxes" in ocr_data:
            for quad_box, label in zip(ocr_data["quad_boxes"], ocr_data["labels"]):
                label = label.strip()
                if not label or len(label) > 60:
                    continue
                xs = [quad_box[0], quad_box[2], quad_box[4], quad_box[6]]
                ys = [quad_box[1], quad_box[3], quad_box[5], quad_box[7]]
                cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)
                if 0 <= cx <= sw and 0 <= cy <= sh:
                    blocks.append({"desc": label, "kind": "text", "cx": cx, "cy": cy})
    except Exception as e:
        print(f"[!] Florence-2 OCR error: {e}")
    return blocks

def vision_region_caption(screenshot):
    """Region caption using local Florence-2 model."""
    sw, sh = screenshot.size
    regions = []
    try:
        task_prompt = "<DENSE_REGION_CAPTION>"
        inputs = local_vision_processor(text=task_prompt, images=screenshot, return_tensors="pt").to(device, torch_dtype)
        with torch.no_grad():
            ids = local_vision_model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=512, num_beams=3
            )
        raw = local_vision_processor.batch_decode(ids, skip_special_tokens=False)[0]
        parsed = local_vision_processor.post_process_generation(raw, task=task_prompt, image_size=screenshot.size)
        region_data = parsed.get(task_prompt, {})
        if region_data and "labels" in region_data and "bboxes" in region_data:
            for label, bbox in zip(region_data["labels"], region_data["bboxes"]):
                label = label.strip()
                if not label or len(label) > 80:
                    continue
                cx = int((bbox[0] + bbox[2]) / 2)
                cy = int((bbox[1] + bbox[3]) / 2)
                width = int(bbox[2] - bbox[0])
                height = int(bbox[3] - bbox[1])
                if 0 <= cx <= sw and 0 <= cy <= sh:
                    regions.append({"desc": label, "kind": "image", "cx": cx, "cy": cy, "width": width, "height": height})
    except Exception as e:
        print(f"[!] Florence-2 region caption error: {e}")
    return regions

PROFILE_KEYWORDS = ["profile", "avatar", "user", "account", "person", "face"]

# ==========================================
# MULTI-TIER GROUNDING LOCATOR ENGINE
# ==========================================
import uiautomation as auto

def find_via_ui_automation(target_label):
    """Tier 1: Instant OS control inspection via Windows UI Automation Tree."""
    try:
        active_window = auto.GetForegroundControl()
        if not active_window:
            return None
            
        print(f"[*] Tier 1: Scanning UI Automation tree under: {active_window.Name}...")
        
        # =================================
        # UNIVERSAL PROFILE/AVATAR DETECTION
        # =================================
        target_lower = target_label.lower().strip()
        profile_keywords = PROFILE_KEYWORDS
        is_profile_search = any(kw in target_lower for kw in profile_keywords)
        
        if is_profile_search:
            print(f"[*] Enhanced profile/avatar detection: {target_label}")
            for control, depth in auto.WalkControl(active_window, maxDepth=6):
                name = control.Name.lower() if control.Name else ""
                auto_id = control.AutomationId.lower() if control.AutomationId else ""
                class_name = control.ClassName.lower() if control.ClassName else ""
                control_type = control.ControlTypeName.lower() if control.ControlTypeName else ""
                
                try:
                    rect = control.BoundingRectangle
                    width = rect.Width()
                    height = rect.Height()
                    center_x = rect.left + width // 2
                    center_y = rect.top + height // 2
                except Exception:
                    continue
                
                if width <= 0 or height <= 0:
                    continue
                
                is_profile_candidate = False
                confidence = 0.0
                screen_w, screen_h = pyautogui.size()
                
                if any(kw in name or kw in auto_id for kw in profile_keywords):
                    is_profile_candidate = True
                    confidence = 0.9
                elif "image" in control_type and width < 200 and height < 200:
                    if any(kw in name or kw in auto_id for kw in ["user", "person", "account", "face", "photo"]):
                        is_profile_candidate = True
                        confidence = 0.75
                elif "button" in control_type and center_y < screen_h * 0.4:
                    if any(kw in name or kw in auto_id for kw in ["profile", "avatar", "user"]):
                        is_profile_candidate = True
                        confidence = 0.8
                
                if is_profile_candidate:
                    screen_w, screen_h = pyautogui.size()
                    if 0 <= center_x <= screen_w and 0 <= center_y <= screen_h:
                        print(f"[🎯 PROFILE MATCH] Found via UI Automation: '{control.Name}' at ({center_x}, {center_y}) [Conf: {confidence:.2f}]")
                        return (center_x, center_y, confidence)
        
        # =================================
        # EXISTING SPECIALIZED DETECTION (Keep for backward compatibility)
        # =================================
        
        # Check if this is a VS Code window
        is_vscode = False
        window_name = active_window.Name.lower() if active_window.Name else ""
        if "visual studio code" in window_name or "code - " in window_name or "vscode" in window_name:
            is_vscode = True
            print(f"[*] VS Code window detected: {active_window.Name}")
        
        # Walk active window tree to check if this is the Chrome Profile Picker window
        is_profile_picker = False
        if "chrome" in window_name or "who's using" in window_name:
            has_guest = False
            has_add = False
            for control, depth in auto.WalkControl(active_window, maxDepth=3):
                name_lower = control.Name.lower() if control.Name else ""
                if "guest" in name_lower:
                    has_guest = True
                if "add profile" in name_lower or "add" in name_lower:
                    has_add = True
            if has_guest or has_add:
                is_profile_picker = True
                
        # Special heuristic for clicking profile on Chrome profile picker screen
        if target_label.lower().strip() in ["profile", "avatar"] and is_profile_picker:
            print("[*] Chrome Profile Picker screen detected. Locating first user profile card...")
            for control, depth in auto.WalkControl(active_window, maxDepth=4):
                if control.ControlTypeName in ["ButtonControl", "ListItemControl"]:
                    name_lower = control.Name.lower() if control.Name else ""
                    skip_words = ["guest", "add", "manage", "close", "minimize", "maximize", "help", "settings", "feedback"]
                    if name_lower and not any(sw in name_lower for sw in skip_words):
                        try:
                            rect = control.BoundingRectangle
                            if rect.Width() > 0 and rect.Height() > 0:
                                center_x = rect.left + rect.Width() // 2
                                center_y = rect.top + rect.Height() // 2
                                screen_w, screen_h = pyautogui.size()
                                if 0 <= center_x <= screen_w and 0 <= center_y <= screen_h:
                                    print(f"[🎯 PROFILE CARD MATCH] Found profile card: '{control.Name}' at ({center_x}, {center_y})")
                                    return (center_x, center_y)
                        except Exception:
                            continue
        
        # VS Code specific element detection
        if is_vscode:
            vscode_targets = {
                "explorer": ["explorer", "file explorer", "files"],
                "terminal": ["terminal", "panel", "output", "problems", "debug console"],
                "editor": ["editor", "tab", "open editors"],
                "search": ["search", "find in files"],
                "source_control": ["source control", "git", "changes"],
                "extensions": ["extensions", "marketplace"],
                "run": ["run", "debug", "play", "start debugging"],
                "settings": ["settings", "preferences"],
                "command_palette": ["command palette", "command"],
                "new_file": ["new file", "new text file"],
                "save": ["save", "save all"],
                "close_tab": ["close", "x", "close tab"],
                "split_editor": ["split", "split editor"],
                "zen_mode": ["zen", "zen mode"],
                "sidebar": ["sidebar", "activity bar", "primary side bar"],
                "panel": ["panel", "bottom panel"],
                "status_bar": ["status", "status bar"]
            }
            
            target_clean = target_label.lower().strip()
            for vscode_element, keywords in vscode_targets.items():
                if any(kw in target_clean for kw in keywords):
                    print(f"[*] VS Code specific target detected: {vscode_element} (matched: {target_label})")
                    for control, depth in auto.WalkControl(active_window, maxDepth=5):
                        name = control.Name.lower() if control.Name else ""
                        auto_id = control.AutomationId.lower() if control.AutomationId else ""
                        class_name = control.ClassName.lower() if control.ClassName else ""
                        
                        # Check various VS Code control identifiers
                        match_found = False
                        if vscode_element == "explorer" and ("explorer" in name or "explorer" in auto_id or "files" in name):
                            match_found = True
                        elif vscode_element == "terminal" and ("terminal" in name or "terminal" in auto_id or "panel" in name):
                            match_found = True
                        elif vscode_element == "search" and ("search" in name or "search" in auto_id):
                            match_found = True
                        elif vscode_element == "source_control" and ("source control" in name or "git" in name or "changes" in name):
                            match_found = True
                        elif vscode_element == "extensions" and ("extension" in name or "marketplace" in name):
                            match_found = True
                        elif vscode_element == "run" and ("run" in name or "debug" in name or "play" in name):
                            match_found = True
                        elif vscode_element == "settings" and ("setting" in name or "preferences" in name):
                            match_found = True
                        elif vscode_element == "command_palette" and ("command" in name or "palette" in name):
                            match_found = True
                        elif vscode_element == "new_file" and ("new" in name and ("file" in name or "text" in name)):
                            match_found = True
                        elif vscode_element in ["save", "close_tab", "split_editor", "zen_mode", "sidebar", "panel", "status_bar"]:
                            if vscode_element.replace("_", " ") in name or vscode_element in auto_id:
                                match_found = True
                        elif target_label in name or target_label in auto_id:
                            match_found = True
                            
                        if match_found:
                            try:
                                rect = control.BoundingRectangle
                                if rect.Width() > 0 and rect.Height() > 0:
                                    center_x = rect.left + rect.Width() // 2
                                    center_y = rect.top + rect.Height() // 2
                                    screen_w, screen_h = pyautogui.size()
                                    if 0 <= center_x <= screen_w and 0 <= center_y <= screen_h:
                                        print(f"[🎯 VSCODE MATCH] Found '{vscode_element}' at ({center_x}, {center_y})")
                                        return (center_x, center_y)
                            except Exception:
                                continue
        
        # Browser specific element detection (Chrome, Edge, Firefox)
        is_browser = False
        browser_name = ""
        if "chrome" in window_name:
            is_browser = True
            browser_name = "Chrome"
        elif "edge" in window_name or "microsoft edge" in window_name:
            is_browser = True
            browser_name = "Edge"
        elif "firefox" in window_name or "mozilla" in window_name:
            is_browser = True
            browser_name = "Firefox"
        elif "brave" in window_name:
            is_browser = True
            browser_name = "Brave"
            
        if is_browser:
            print(f"[*] {browser_name} browser window detected: {active_window.Name}")
            browser_targets = {
                "address_bar": ["address", "url", "omnibox", "search bar", "address bar"],
                "new_tab": ["new tab", "tab", "+"],
                "close_tab": ["close", "x"],
                "refresh": ["refresh", "reload"],
                "back": ["back", "previous"],
                "forward": ["forward", "next"],
                "home": ["home"],
                "bookmarks": ["bookmark", "favorite"],
                "menu": ["menu", "more", "settings", "three dots", "hamburger"],
                "downloads": ["download"],
                "history": ["history"],
                "extensions": ["extension", "addon"],
            }
            
            target_clean = target_label.lower().strip()
            for browser_element, keywords in browser_targets.items():
                if any(kw in target_clean for kw in keywords):
                    print(f"[*] {browser_name} specific target detected: {browser_element} (matched: {target_label})")
                    for control, depth in auto.WalkControl(active_window, maxDepth=5):
                        name = control.Name.lower() if control.Name else ""
                        auto_id = control.AutomationId.lower() if control.AutomationId else ""
                        class_name = control.ClassName.lower() if control.ClassName else ""
                        
                        match_found = False
                        if browser_element == "address_bar":
                            # Address bar typically has class "Chrome_OmniboxView" or similar
                            if any(x in class_name for x in ["omnibox", "address", "url", "edit"]):
                                match_found = True
                            elif "address" in name or "url" in name or "search" in name:
                                match_found = True
                            # Also check by position - address bar is usually at top
                            rect = control.BoundingRectangle
                            if rect.Width() > 0 and rect.Height() > 0:
                                center_y = rect.top + rect.Height() // 2
                                screen_h = pyautogui.size()[1]
                                if center_y < screen_h * 0.15:  # Top 15% of screen
                                    if any(x in name for x in ["edit", "text", "address", "url", "search"]) or control.ControlTypeName == "EditControl":
                                        match_found = True
                        elif browser_element == "new_tab" and ("new" in name or "tab" in name or "+" in name):
                            match_found = True
                        elif browser_element == "close_tab" and ("close" in name or name == "x"):
                            match_found = True
                        elif browser_element == "refresh" and ("refresh" in name or "reload" in name):
                            match_found = True
                        elif browser_element == "back" and ("back" in name or "previous" in name):
                            match_found = True
                        elif browser_element == "forward" and ("forward" in name or "next" in name):
                            match_found = True
                        elif browser_element == "menu" and ("menu" in name or "more" in name or "settings" in name or "..." in name):
                            match_found = True
                            
                        if match_found:
                            try:
                                rect = control.BoundingRectangle
                                if rect.Width() > 0 and rect.Height() > 0:
                                    center_x = rect.left + rect.Width() // 2
                                    center_y = rect.top + rect.Height() // 2
                                    screen_w, screen_h = pyautogui.size()
                                    if 0 <= center_x <= screen_w and 0 <= center_y <= screen_h:
                                        print(f"[🎯 BROWSER MATCH] Found '{browser_element}' at ({center_x}, {center_y})")
                                        return (center_x, center_y)
                            except Exception:
                                continue
        
        # Walk active window tree for broader UI element matching with deeper depth
        target_lower = target_label.lower().strip().replace("_", " ").replace("-", " ")
        
        for control, depth in auto.WalkControl(active_window, maxDepth=6):
            name = control.Name.lower() if control.Name else ""
            auto_id = control.AutomationId.lower() if control.AutomationId else ""
            class_name = control.ClassName.lower() if control.ClassName else ""
            control_type = control.ControlTypeName.lower() if control.ControlTypeName else ""
            
            rect = control.BoundingRectangle
            try:
                width, height = rect.Width(), rect.Height()
                center_x = rect.left + width // 2
                center_y = rect.top + height // 2
            except Exception:
                continue
            
            if width <= 0 or height <= 0:
                continue
            if center_x < 0 or center_y < 0:
                continue
            
            screen_w, screen_h = pyautogui.size()
            if not (0 <= center_x <= screen_w and 0 <= center_y <= screen_h):
                continue
            
            # Skip noise controls
            if control_type in ["window", "pane", "scrollbar", "separator"]:
                continue
            if width < 10 or height < 5:
                continue
            
            # Check name and auto_id with flexible matching
            name_match = target_lower in name or name in target_lower
            auto_id_match = target_lower in auto_id or auto_id in target_lower
            
            # Keyword matching for common targets
            keyword_match = False
            if target_lower in ["address_bar", "address bar", "url", "omnibox", "search bar"]:
                if any(x in name for x in ["address", "url", "omnibox", "search"]) or control_type == "edit":
                    keyword_match = True
            elif target_lower in ["watch", "play", "video"]:
                if any(x in name for x in ["watch", "play", "video", "media"]):
                    keyword_match = True
            elif target_lower in ["full screen", "fullscreen"]:
                if any(x in name for x in ["full", "expand", "max"]):
                    keyword_match = True
            elif target_lower in ["menu", "settings", "more"]:
                if any(x in name for x in ["menu", "setting", "more", "option", "three"]):
                    keyword_match = True
            
            if name_match or auto_id_match or keyword_match:
                print(f"[*] UI Automation: Found '{target_label}' as '{name}' ({control_type}) at ({center_x}, {center_y}) [depth:{depth}]")
                return (center_x, center_y)
    
    except Exception as e:
        print(f"[!] UI Automation error: {e}")
    return None

def find_via_template_matching(target_label):
    """Tier 2: Classic pixel matrix pattern match using local crop files with confidence scoring."""
    TARGET_FOLDER = "./insts"
    if not os.path.exists(TARGET_FOLDER):
        return None, None
        
    template_name = target_label if target_label.endswith(".png") else f"{target_label}.png"
    template_path = os.path.join(TARGET_FOLDER, template_name)
    if not os.path.exists(template_path):
        return None, None
        
    print(f"[*] Tier 2: Running Enhanced OpenCV Template Match for '{template_name}'...")
    try:
        screenshot = pyautogui.screenshot()
        screen_bgr = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        template = cv2.imread(template_path)
        if template is None:
            return None, None
            
        h, w = template.shape[:2]
        result = cv2.matchTemplate(screen_bgr, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        
        if max_val >= 0.8:
            confidence = max_val
            print(f"[*] Template match confidence: {confidence:.2f}")
            return max_loc[0] + w // 2, max_loc[1] + h // 2, confidence
    except Exception as e:
        print(f"[!] Template matching error: {e}")
    return None, None

import requests as _requests

def _detect_best_ollama_model(preferred_models=None):
    """Pick the best available local Ollama model from the installed list or .env override."""
    env_model = os.getenv("OLLAMA_MODEL")
    if env_model:
        print(f"[🧠 MODEL] Using OLLAMA_MODEL from env: {env_model}")
        return env_model

    if preferred_models is None:
        preferred_models = [
            "llama3.1:8b",
            "qwen2.5:7b",
            "qwen2.5:14b",
            "mistral:7b",
            "gemma2:9b",
            "phi3:14b",
            "llama3:8b",
        ]
    try:
        base = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        resp = _requests.get(f"{base}/api/tags", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            available = {m.get("name", "") for m in data.get("models", [])}
            for model in preferred_models:
                if model in available:
                    print(f"[🧠 MODEL] Using detected Ollama model: {model}")
                    return model
            names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            if names:
                print(f"[🧠 MODEL] Falling back to first available Ollama model: {names[0]}")
                return names[0]
    except Exception:
        pass
    print("[🧠 MODEL] Falling back to default model: llama3:8b")
    return "llama3:8b"

OLLAMA_MODEL = _detect_best_ollama_model()

def ollama_chat(model, messages, temperature=0.0, max_tokens=None):
    """Local Ollama API call for text reasoning."""
    try:
        ollama_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            ollama_messages.append({"role": role, "content": content})
        
        options = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        
        effective_model = model or OLLAMA_MODEL
        response = ollama.chat(model=effective_model, messages=ollama_messages, options=options)
        return {"choices": [{"message": {"content": response["message"]["content"]}}]}
    except Exception as e:
        print(f"[!] Ollama error: {e}")
        return {"choices": [{"message": {"content": ""}}]}



import re as _re

def find_via_ocr(target_label):
    """Tier 3: Local Florence-2 OCR + Region Semantic Grounding + Ollama selection."""
    print("[*] Tier 3: Running Local Florence-2 OCR + Semantic Grounding...")
    blocks = []
    try:
        screenshot = pyautogui.screenshot().convert("RGB")
        sw, sh = screenshot.size
        target_lower = target_label.lower().strip()
        is_address_bar = target_lower in ["address_bar", "address bar", "url", "omnibox", "search bar"]

        # --- Pass A: Dense Region Caption (image-based UI elements) ---
        try:
            task_a = "<DENSE_REGION_CAPTION>"
            inputs_a = local_vision_processor(text=task_a, images=screenshot, return_tensors="pt").to(device, torch_dtype)
            with torch.no_grad():
                ids_a = local_vision_model.generate(
                    input_ids=inputs_a["input_ids"],
                    pixel_values=inputs_a["pixel_values"],
                    max_new_tokens=512, num_beams=3
                )
            raw_a = local_vision_processor.batch_decode(ids_a, skip_special_tokens=False)[0]
            parsed_a = local_vision_processor.post_process_generation(raw_a, task=task_a, image_size=screenshot.size)
            region_data = parsed_a.get(task_a, {})
            if region_data and "labels" in region_data and "bboxes" in region_data:
                for label, bbox in zip(region_data["labels"], region_data["bboxes"]):
                    label = label.strip()
                    if not label or len(label) > 80:
                        continue
                    if not any(c.isalpha() for c in label):
                        continue
                    cx = int((bbox[0] + bbox[2]) / 2)
                    cy = int((bbox[1] + bbox[3]) / 2)
                    if is_address_bar and cy > sh * 0.20:
                        continue
                    if 0 <= cx <= sw and 0 <= cy <= sh:
                        blocks.append({"desc": label, "kind": "image", "cx": cx, "cy": cy})
        except Exception as e_a:
            print(f"[!] Dense Region Caption pass failed: {e_a}")

        # --- Pass B: OCR (text-labelled UI elements) ---
        try:
            task_b = "<OCR_WITH_REGION>"
            inputs_b = local_vision_processor(text=task_b, images=screenshot, return_tensors="pt").to(device, torch_dtype)
            with torch.no_grad():
                ids_b = local_vision_model.generate(
                    input_ids=inputs_b["input_ids"],
                    pixel_values=inputs_b["pixel_values"],
                    max_new_tokens=1024, num_beams=3
                )
            raw_b = local_vision_processor.batch_decode(ids_b, skip_special_tokens=False)[0]
            parsed_b = local_vision_processor.post_process_generation(raw_b, task=task_b, image_size=screenshot.size)
            ocr_data = parsed_b.get(task_b, {})
            if ocr_data and "labels" in ocr_data and "quad_boxes" in ocr_data:
                for quad_box, label in zip(ocr_data["quad_boxes"], ocr_data["labels"]):
                    label = label.strip()
                    if not label or len(label) > 60:
                        continue
                    if not any(c.isalpha() for c in label):
                        continue
                    xs = [quad_box[0], quad_box[2], quad_box[4], quad_box[6]]
                    ys = [quad_box[1], quad_box[3], quad_box[5], quad_box[7]]
                    cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)
                    if is_address_bar and cy > sh * 0.20:
                        continue
                    if is_address_bar:
                        label_lower = label.lower()
                        skip_patterns = [
                            "dell", "hp", "lenovo", "intel", "nvidia", "amd", "windows",
                            "profile", "avatar", "guest", "add profile", "manage",
                            "settings", "menu", "more", "bookmark", "history", "download",
                            "extension", "new tab", "close", "refresh", "back", "forward", "home",
                            "0.0.0", "0.1.0", "-0.", "://", "http", "www", "youtube", "google",
                            "search", "tab", "microsoft", "chrome", "edge", "firefox"
                        ]
                        if any(skip in label_lower for skip in skip_patterns):
                            continue
                    if 0 <= cx <= sw and 0 <= cy <= sh:
                        blocks.append({"desc": label, "kind": "text", "cx": cx, "cy": cy})
        except Exception as e_b:
            print(f"[!] OCR pass failed: {e_b}")

        if not blocks:
            ui_coords = find_via_ui_automation(target_label)
            if ui_coords:
                return ui_coords[0], ui_coords[1]
            return None

        # --- Direct text match fallback (before Ollama) ---
        target_words = set(target_lower.replace("thumbnail", "").replace("video", "").replace("click", "").split())
        target_words = {w for w in target_words if len(w) > 2}
        
        for i, block in enumerate(blocks):
            if block["kind"] == "text":
                block_words = set(block["desc"].lower().split())
                if target_words and target_words.issubset(block_words):
                    print(f"[*] Tier 3 (direct text match): Found '{block['desc']}' at ({block['cx']}, {block['cy']})")
                    return (block["cx"], block["cy"])

        # --- Send merged list to Ollama for semantic selection ---
        block_list_str = "\n".join(
            f"{i} [{blk['kind']}]: {blk['desc']}" for i, blk in enumerate(blocks)
        )
        print(f"[*] Tier 3 (semantic): {len(blocks)} regions/blocks. Asking Ollama which matches '{target_label}'...")

        ollama_prompt = (
            f"You are a UI element selector. The user wants to click on: '{target_label}'.\n"
            f"Below is a numbered list of UI regions currently visible on screen.\n"
            f"Entries marked [image] are visual regions (icons, cards, avatars, buttons with images).\n"
            f"Entries marked [text] are text labels (menu items, tab names, button text).\n\n"
            f"{block_list_str}\n\n"
            f"Which numbered item is the BEST element to click to accomplish '{target_label}'?\n"
            f"Consider semantic meaning, not just word match. Reply with ONLY the integer index. Nothing else."
        )

        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": ollama_prompt}],
            options={"temperature": 0.0}
        )
        raw = response['message']['content'].strip()
        import re as _re3
        m = _re3.search(r'\d+', raw)
        if m:
            idx = int(m.group())
            if 0 <= idx < len(blocks):
                chosen = blocks[idx]
                print(f"[*] Tier 3 (semantic): Ollama picked index {idx} [{chosen['kind']}] '{chosen['desc']}' at ({chosen['cx']}, {chosen['cy']})")
                return chosen["cx"], chosen["cy"]
    except Exception as e:
        print(f"[!] Semantic grounding error: {e}")
    return None

def find_via_phrase_grounding(target_label):
    """Tier 4: Local Florence-2 phrase grounding for descriptive visual targets."""
    print(f"[*] Tier 4: Querying Florence-2 phrase grounding for: '{target_label}'...")
    try:
        screenshot = pyautogui.screenshot().convert("RGB")
        sw, sh = screenshot.size
        
        is_address_bar = target_label.lower().strip() in ["address_bar", "address bar", "url", "omnibox", "search bar"]
        search_image = screenshot
        if is_address_bar:
            search_image = screenshot.crop((0, 0, sw, int(sh * 0.25)))
            print(f"[*] Tier 4: Cropping to top 25% for address bar detection")
        
        task_prompt = "<CAPTION_TO_PHRASE_GROUNDING>"
        full_prompt = task_prompt + target_label
        inputs = local_vision_processor(text=full_prompt, images=search_image, return_tensors="pt").to(device, torch_dtype)
        
        with torch.no_grad():
            generated_ids = local_vision_model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=256,
                num_beams=3
            )
            
        results = local_vision_processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
        parsed_answer = local_vision_processor.post_process_generation(results, task=task_prompt, image_size=search_image.size)
        grounding_data = parsed_answer.get(task_prompt, {})
        
        if grounding_data and "boxes" in grounding_data and len(grounding_data["boxes"]) > 0:
            box = grounding_data["boxes"][0]
            cx = int((box[0] + box[2]) / 2)
            cy = int((box[1] + box[3]) / 2)
            return cx, cy
    except Exception as e:
        print(f"[!] Phrase grounding error: {e}")
    return None, None

def find_via_zoom_quadrants(target_label):
    """Tier 5: High Resolution quadrant division search via local Florence-2."""
    print("[*] Tier 5: Slicing canvas into overlapping quadrants to scan small details...")
    try:
        screenshot = pyautogui.screenshot().convert("RGB")
        screen_w, screen_h = screenshot.size
        mid_x, mid_y = screen_w // 2, screen_h // 2
        overlap = 150
        
        is_address_bar = target_label.lower().strip() in ["address_bar", "address bar", "url", "omnibox", "search bar"]
        
        quadrants = [
            {"box": (0, 0, mid_x + overlap, mid_y + overlap), "offset": (0, 0), "name": "Top-Left"},
            {"box": (mid_x - overlap, 0, screen_w, mid_y + overlap), "offset": (mid_x - overlap, 0), "name": "Top-Right"},
            {"box": (0, mid_y - overlap, mid_x + overlap, screen_h), "offset": (0, mid_y - overlap), "name": "Bottom-Left"},
            {"box": (mid_x - overlap, mid_y - overlap, screen_w, screen_h), "offset": (mid_x - overlap, mid_y - overlap), "name": "Bottom-Right"}
        ]
        
        if is_address_bar:
            quadrants = [
                {"box": (0, 0, screen_w, int(screen_h * 0.3)), "offset": (0, 0), "name": "Top-AddressBar"}
            ]
            print("[*] Tier 5: Focused scan on top 30% for address bar detection")
        
        target_clean = target_label.lower().strip()
        for quad in quadrants:
            cropped = screenshot.crop(quad["box"])
            sw_q, sh_q = cropped.size
            
            # 5A. OCR on quadrant using local Florence-2
            try:
                task_prompt_ocr = "<OCR_WITH_REGION>"
                inputs = local_vision_processor(text=task_prompt_ocr, images=cropped, return_tensors="pt").to(device, torch_dtype)
                with torch.no_grad():
                    generated_ids = local_vision_model.generate(
                        input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"], 
                        max_new_tokens=1024, num_beams=1
                    )
                results = local_vision_processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
                parsed_answer = local_vision_processor.post_process_generation(results, task=task_prompt_ocr, image_size=cropped.size)
                ocr_data = parsed_answer.get(task_prompt_ocr, {})
                
                if ocr_data and "labels" in ocr_data and "quad_boxes" in ocr_data:
                    for quad_box, label in zip(ocr_data["quad_boxes"], ocr_data["labels"]):
                        label_clean = label.lower().strip()
                        if len(label_clean) < 2:
                            continue
                        if target_clean in label_clean or label_clean in target_clean:
                            xs = [quad_box[0], quad_box[2], quad_box[4], quad_box[6]]
                            ys = [quad_box[1], quad_box[3], quad_box[5], quad_box[7]]
                            center_x = int(sum(xs) / 4) + quad["offset"][0]
                            center_y = int(sum(ys) / 4) + quad["offset"][1]
                            return (center_x, center_y, 0.6)
            except Exception:
                pass
            
            # 5B. Phrase Grounding on quadrant using local Florence-2
            try:
                task_prompt_phrase = "<CAPTION_TO_PHRASE_GROUNDING>"
                full_prompt = task_prompt_phrase + target_label
                inputs = local_vision_processor(text=full_prompt, images=cropped, return_tensors="pt").to(device, torch_dtype)
                with torch.no_grad():
                    generated_ids = local_vision_model.generate(
                        input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"], 
                        max_new_tokens=256, num_beams=1
                    )
                results = local_vision_processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
                parsed_answer = local_vision_processor.post_process_generation(results, task=task_prompt_phrase, image_size=cropped.size)
                grounding_data = parsed_answer.get(task_prompt_phrase, {})
                
                if grounding_data and "boxes" in grounding_data and len(grounding_data["boxes"]) > 0:
                    box = grounding_data["boxes"][0]
                    center_x = int((box[0] + box[2]) / 2) + quad["offset"][0]
                    center_y = int((box[1] + box[3]) / 2) + quad["offset"][1]
                    return (center_x, center_y, 0.55)
            except Exception:
                pass
    except Exception as e:
        print(f"[!] Quadrant zoom lookup failure: {e}")
    return None

def find_element_coordinates(target_label):
    """Consolidated Multi-Tier visual coordinate locator wrapper with confidence scoring."""
    def _valid(coords):
        return coords and len(coords) >= 2 and coords[0] is not None and coords[1] is not None
    
    # For thumbnail/video targets, try visual thumbnail detection FIRST (before text-based tiers)
    # This prevents UI Automation/OCR from returning text labels like channel names
    thumb_lower = target_label.lower().strip()
    thumb_keywords = ["thumbnail", "video", "youtube", "watch", "clip", "movie", "stream"]
    is_thumb_request = any(kw in thumb_lower for kw in thumb_keywords)
    
    if is_thumb_request:
        print(f"[*] Thumbnail/video request detected - leading with visual thumbnail search")
        video_result = find_video_thumbnail(target_label)
        if video_result and _valid(video_result):
            print(f"[🎯 MATCH] Located video thumbnail '{target_label}' at ({video_result[0]}, {video_result[1]}) [conf:{video_result[2]:.2f}] (PRIORITY)")
            return (video_result[0], video_result[1])
        # If thumbnail search didn't find anything, skip text tiers entirely for thumbnail requests
        # to avoid clicking on text labels instead of the actual thumbnail image
        print(f"[*] Thumbnail search returned nothing, skipping text-based tiers for thumbnail request")
        
        # Try visual-only tiers for thumbnail requests
        coords = find_via_template_matching(target_label)
        if _valid(coords):
            conf = coords[2] if len(coords) >= 3 else 0.85
            print(f"[🎯 MATCH] Located '{target_label}' via template matcher: ({coords[0]}, {coords[1]}) [conf:{conf:.2f}]")
            return (coords[0], coords[1])
        
        coords = find_via_phrase_grounding(target_label)
        if _valid(coords):
            conf = 0.65
            print(f"[🎯 MATCH] Located '{target_label}' via phrase grounding: ({coords[0]}, {coords[1]}) [conf:{conf:.2f}]")
            return (coords[0], coords[1])
        
        coords = find_via_zoom_quadrants(target_label)
        if _valid(coords):
            conf = 0.5
            print(f"[🎯 MATCH] Located '{target_label}' via high-resolution zoom scan: ({coords[0]}, {coords[1]}) [conf:{conf:.2f}]")
            return (coords[0], coords[1])
        
        # Second-pass: try thumbnail detection with broader search
        print(f"[*] Second-pass thumbnail search with broader criteria...")
        video_result = find_video_thumbnail(target_label, broad=True)
        if video_result and _valid(video_result):
            print(f"[🎯 MATCH] Located video thumbnail '{target_label}' at ({video_result[0]}, {video_result[1]}) [conf:{video_result[2]:.2f}] (BROAD FALLBACK)")
            return (video_result[0], video_result[1])
        
        return None
    
    best_coords = None
    best_confidence = 0
    
    coords = find_via_ui_automation(target_label)
    if _valid(coords):
        conf = coords[2] if len(coords) >= 3 else 0.9
        if conf > best_confidence:
            best_coords = coords
            best_confidence = conf
            print(f"[🎯 MATCH] Located '{target_label}' via native UI accessibility mapping: ({coords[0]}, {coords[1]}) [conf:{conf:.2f}]")
    
    coords = find_via_template_matching(target_label)
    if _valid(coords):
        conf = coords[2] if len(coords) >= 3 else 0.85
        if conf > best_confidence:
            best_coords = coords
            best_confidence = conf
            print(f"[🎯 MATCH] Located '{target_label}' via template matcher: ({coords[0]}, {coords[1]}) [conf:{conf:.2f}]")
    
    coords = find_via_ocr(target_label)
    if _valid(coords):
        conf = 0.7
        if conf > best_confidence:
            best_coords = coords
            best_confidence = conf
            print(f"[🎯 MATCH] Located '{target_label}' via visual OCR analysis: ({coords[0]}, {coords[1]}) [conf:{conf:.2f}]")
    
    coords = find_via_phrase_grounding(target_label)
    if _valid(coords):
        conf = 0.65
        if conf > best_confidence:
            best_coords = coords
            best_confidence = conf
            print(f"[🎯 MATCH] Located '{target_label}' via phrase grounding: ({coords[0]}, {coords[1]}) [conf:{conf:.2f}]")
    
    coords = find_via_zoom_quadrants(target_label)
    if _valid(coords):
        conf = 0.5
        if conf > best_confidence:
            best_coords = coords
            best_confidence = conf
            print(f"[🎯 MATCH] Located '{target_label}' via high-resolution zoom scan: ({coords[0]}, {coords[1]}) [conf:{conf:.2f}]")
    
    if _valid(best_coords):
        return (best_coords[0], best_coords[1])
    
    # Fallback to video thumbnail if first pass missed
    if is_thumb_request:
        video_result = find_video_thumbnail(target_label)
        if video_result and _valid(video_result):
            print(f"[🎯 MATCH] Located video thumbnail '{target_label}' at ({video_result[0]}, {video_result[1]}) [conf:{video_result[2]:.2f}] (FALLBACK)")
            return (video_result[0], video_result[1])
    
    return None

def find_video_thumbnail(target_label, broad=False):
    """Specialized detection for YouTube/video thumbnails using visual region analysis."""
    target_lower = target_label.lower().strip()
    video_keywords = ["video", "youtube", "watch", "thumbnail", "movie", "clip", "stream"]
    
    if not any(kw in target_lower for kw in video_keywords):
        return None
    
    print(f"[*] Video thumbnail detection for: {target_label} (broad={broad})")
    try:
        screenshot = pyautogui.screenshot().convert("RGB")
        sw, sh = screenshot.size
        
        # For YouTube, scan the full screen but exclude top bar
        search_image = screenshot
        if "youtube" in target_lower or "video" in target_lower:
            search_image = screenshot.crop((0, int(sh * 0.08), sw, sh))
        
        regions = vision_region_caption(search_image)
        
        # Extract search terms from target (e.g., "markplier" from "markplier thumbnail video")
        search_terms = []
        stop_words = {"thumbnail", "video", "watch", "clip", "movie", "stream", "youtube", "image", "photo", "screen", "play", "click", "the", "a", "an", "is", "in", "on", "at", "to", "for", "of", "and", "or", "with", "by"}
        for word in target_lower.split():
            word = word.strip().strip("'").strip('"')
            if word and len(word) > 2 and word not in stop_words:
                search_terms.append(word)
        
        print(f"[*] Thumbnail search terms: {search_terms}")
        
        video_candidates = []
        thumbnail_labels = ["video", "thumbnail", "image", "photo", "clip", "movie", "screen", "play", "card", "poster", "preview"]
        
        aspect_min = 1.0 if broad else 1.2
        aspect_max = 3.0 if broad else 2.5
        min_width = 60 if broad else 100
        min_height = 40 if broad else 50
        
        for r in regions:
            label = r["desc"].lower()
            cx, cy = r["cx"], r["cy"]
            width = r.get("width", 200)
            height = r.get("height", 200)
            aspect = width / height if height > 0 else 0
            
            is_wide = aspect_min < aspect < aspect_max
            is_reasonable_size = width > min_width and height > min_height and width < 600 and height < 400
            
            term_match = any(term in label for term in search_terms) if search_terms else False
            label_match = any(kw in label for kw in thumbnail_labels)
            
            if is_wide and is_reasonable_size:
                if term_match and label_match:
                    video_candidates.append((cx, cy, 0.95, r["desc"]))
                elif term_match:
                    video_candidates.append((cx, cy, 0.85, r["desc"]))
                elif label_match:
                    video_candidates.append((cx, cy, 0.6, r["desc"]))
                else:
                    # Unknown visual region but has right shape - could be thumbnail
                    video_candidates.append((cx, cy, 0.4, r["desc"]))
        
        if not video_candidates and search_terms:
            for r in regions:
                label = r["desc"].lower()
                cx, cy = r["cx"], r["cy"]
                width = r.get("width", 200)
                height = r.get("height", 200)
                aspect = width / height if height > 0 else 0
                
                if any(term in label for term in search_terms) and 0.5 < aspect < 3.0 and width > 60:
                    confidence = 0.5
                    video_candidates.append((cx, cy, confidence, r["desc"]))
        
        if not video_candidates and regions:
            best_area = 0
            best_candidate = None
            for r in regions:
                w = r.get("width", 0)
                h = r.get("height", 0)
                area = w * h
                if area > best_area and w > 100 and h > 50:
                    best_area = area
                    best_candidate = r
            
            if best_candidate:
                confidence = min(0.4, best_area / (sw * sh) * 5)
                video_candidates.append((best_candidate["cx"], best_candidate["cy"], confidence, best_candidate["desc"]))
        
        video_candidates.sort(key=lambda v: v[2], reverse=True)
        if video_candidates:
            top = video_candidates[0]
            print(f"[*] Best video candidate: '{top[3]}' at ({top[0]}, {top[1]}) [conf:{top[2]:.2f}]")
            print(f"[*] Total regions considered: {len(regions)}, candidates: {len(video_candidates)}")
            return (top[0], top[1], top[2])
        else:
            print(f"[!] No video candidates found. Regions found: {len(regions)}")
            if regions:
                print(f"[!] Sample region labels: {[r['desc'][:50] for r in regions[:5]]}")
    except Exception as e:
        print(f"[!] Video thumbnail detection error: {e}")
    return None

# ==========================================
# SMART FALLBACK & NAVIGATION ENGINE
# ==========================================

_element_cache = {}
_retry_history = {}  # Track retry attempts per element: {element: attempt_count}
_expectations = {}  # Track expectations per element
_step_history = []  # Track history of all steps: [{step, prompt, success, error, screen_state}]

def record_step(step, prompt, success, error=None, screen_state=None):
    """Record a step execution in history for context tracking."""
    _step_history.append({
        "step": step,
        "prompt": prompt,
        "success": success,
        "error": error,
        "screen_state": screen_state[:200] if screen_state else None
    })
    # Keep only last 10 steps to prevent memory bloat
    if len(_step_history) > 10:
        _step_history.pop(0)

def set_expectation(element_label, expected_indicators):
    """Set what we expect to see after clicking an element."""
    _expectations[element_label] = expected_indicators
    return True

def check_expectation(element_label, timeout=2.0):
    """Check if expectation was met after action. Returns True if expectation verified."""
    if element_label not in _expectations:
        return True  # No expectation set, assume success
    
    expected = _expectations[element_label]
    
    # Get current screen state
    import uiautomation as auto
    try:
        active_window = auto.GetForegroundControl()
        if not active_window:
            return False
        
        window_name = active_window.Name.lower() if active_window.Name else ""
        
        # Check window title expectations
        if "window_title" in expected:
            if expected["window_title"].lower() not in window_name:
                print(f"[❌ EXPECT] Window title mismatch. Expected '{expected['window_title']}', got '{window_name}'")
                return False
        
        if "text_contains" in expected or "text_contains_any" in expected:
            screenshot = pyautogui.screenshot().convert("RGB")
            text_blocks = vision_ocr(screenshot)
            all_text = " ".join([b["desc"] for b in text_blocks]).lower()
            
            if "text_contains" in expected:
                for text in expected["text_contains"]:
                    if text.lower() not in all_text:
                        print(f"[❌ EXPECT] Expected text '{text}' not found on screen")
                        return False
            
            if "text_contains_any" in expected:
                if not any(text.lower() in all_text for text in expected["text_contains_any"]):
                    print(f"[❌ EXPECT] None of expected texts found: {expected['text_contains_any']}")
                    return False
            
            print(f"[✅ EXPECT] All expectations verified for '{element_label}'")
            return True
    except Exception as e:
        print(f"[!] Expectation check error: {e}")
    
    return True

def clear_expectation(element_label):
    """Clear an expectation after it's been verified or failed."""
    if element_label in _expectations:
        del _expectations[element_label]

def smart_find_element(target_label):
    """Enhanced element finder with caching, fallbacks, and confidence scoring."""
    global _element_cache
    cache_key = target_label.lower().strip()
    
    if cache_key in _element_cache:
        cached = _element_cache[cache_key]
        if time.time() - cached["timestamp"] < 3.0:
            print(f"[💾 CACHE] Using cached position for '{target_label}': {cached['coords']}")
            return cached["coords"]
        else:
            del _element_cache[cache_key]
    
    coords = find_element_coordinates(target_label)
    if coords and coords[0] is not None and coords[1] is not None:
        _element_cache[cache_key] = {"coords": coords, "timestamp": time.time()}
        return coords
    
    alt_labels = _get_alternative_labels(target_label)
    for alt in alt_labels[:3]:
        coords = find_element_coordinates(alt)
        if coords and coords[0] is not None and coords[1] is not None:
            print(f"[🔄 ALT MATCH] Found '{alt}' for '{target_label}'")
            _element_cache[cache_key] = {"coords": coords, "timestamp": time.time()}
            return coords
    
    navigable_elements = ["button", "link", "tab", "menu", "item", "option", "checkbox", "radio"]
    target_lower = target_label.lower().strip()
    is_navigable = any(elem in target_lower for elem in navigable_elements)
    
    if is_navigable:
        print(f"[⌨️ NAV FALLBACK] Trying keyboard navigation for: {target_label}")
        nav_coords = navigate_with_keyboard(target_label)
        if nav_coords:
            _element_cache[cache_key] = {"coords": nav_coords, "timestamp": time.time()}
            return nav_coords
    
    return None

def navigate_with_keyboard(target_label):
    """Uses Tab, Arrow keys, and Enter to navigate to elements when visual detection fails."""
    try:
        # Take screenshot before navigation
        before = pyautogui.screenshot()
        
        # Strategy: Tab through elements until we likely hit the target
        # For buttons/links: Tab key works
        # For menu items: Arrow keys work
        # For lists: Arrow keys work
        
        max_tabs = 20
        screen_w, screen_h = pyautogui.size()
        center_x, center_y = screen_w // 2, screen_h // 2
        
        # First, try clicking center of screen as anchor point
        pyautogui.click(center_x, center_y)
        time.sleep(0.3)
        
        # Try tabbing forward
        for i in range(max_tabs):
            pyautogui.press('tab')
            time.sleep(0.1)
            
            # Check if cursor/focus moved by taking quick screenshot comparison
            if i % 5 == 0:
                after = pyautogui.screenshot()
                if after != before:
                    print(f"[⌨️ NAV] Focus moved after {i+1} tabs")
                    break
        
        # Try to find element by pressing Enter and checking result
        pyautogui.press('enter')
        time.sleep(0.5)
        
        # Verify if the action had visible effect
        if verify_state_change():
            print(f"[⌨️ NAV] Successfully navigated to: {target_label}")
            return center_x, center_y
        
        return None
    except Exception as e:
        print(f"[!] Keyboard navigation error: {e}")
        return None

_previous_screenshot = None

def detect_screen_context(context=None):
    """Run a full screen analysis: active window + OCR + regions, store in context."""
    global _screen_cache, _screen_cache_time
    if context is None:
        context = {}
    
    import time as _time
    now = _time.time()
    cache = getattr(detect_screen_context, "_cache", None)
    cache_time = getattr(detect_screen_context, "_cache_time", 0)
    
    if cache and (now - cache_time) < 2.0:
        context.update(cache)
        return context
    
    if context is None:
        context = {}
    
    print("[🔍 SCREEN_ANALYZE] Analyzing current screen state...")
    try:
        import uiautomation as auto
        
        active_window = auto.GetForegroundControl()
        window_name = active_window.Name if active_window else "(no active window)"
        context["screen_active_window"] = window_name
        
        wl = window_name.lower()
        if any(k in wl for k in ["visual studio code", "code - ", "vscode", "antigravity"]):
            context["screen_is_ide"] = True
            if "visual studio code" in wl or "code - " in wl or "vscode" in wl:
                context["screen_ide_type"] = "vscode"
            else:
                context["screen_ide_type"] = "antigravity"
        elif any(k in wl for k in ["chrome", "edge", "firefox", "brave"]):
            context["screen_is_ide"] = False
            context["screen_is_browser"] = True
            if "youtube" in wl:
                context["screen_is_youtube"] = True
            else:
                context["screen_is_youtube"] = False
        else:
            context["screen_is_ide"] = False
            context["screen_is_browser"] = False
            context["screen_is_youtube"] = False
        
        text_blocks = []
        region_labels = []
        
        try:
            screenshot = pyautogui.screenshot().convert("RGB")
            sw, sh = screenshot.size
            
            try:
                task_b = "<OCR_WITH_REGION>"
                inputs_b = local_vision_processor(text=task_b, images=screenshot, return_tensors="pt").to(device, torch_dtype)
                with torch.no_grad():
                    ids_b = local_vision_model.generate(
                        input_ids=inputs_b["input_ids"],
                        pixel_values=inputs_b["pixel_values"],
                        max_new_tokens=1024, num_beams=1
                    )
                raw_b = local_vision_processor.batch_decode(ids_b, skip_special_tokens=False)[0]
                parsed_b = local_vision_processor.post_process_generation(raw_b, task=task_b, image_size=screenshot.size)
                ocr_data = parsed_b.get(task_b, {})
                if ocr_data and "labels" in ocr_data and "quad_boxes" in ocr_data:
                    for quad_box, label in zip(ocr_data["quad_boxes"], ocr_data["labels"]):
                        label = label.strip()
                        if not label or len(label) > 80:
                            continue
                        if not any(c.isalpha() for c in label):
                            continue
                        cx = int((quad_box[0] + quad_box[2] + quad_box[4] + quad_box[6]) / 4)
                        cy = int((quad_box[1] + quad_box[3] + quad_box[5] + quad_box[7]) / 4)
                        if 0 <= cx <= sw and 0 <= cy <= sh:
                            text_blocks.append(f"[{cx},{cy}] \"{label}\"")
            except Exception:
                pass
            
            try:
                task_a = "<DENSE_REGION_CAPTION>"
                inputs_a = local_vision_processor(text=task_a, images=screenshot, return_tensors="pt").to(device, torch_dtype)
                with torch.no_grad():
                    ids_a = local_vision_model.generate(
                        input_ids=inputs_a["input_ids"],
                        pixel_values=inputs_a["pixel_values"],
                        max_new_tokens=256, num_beams=1
                    )
                raw_a = local_vision_processor.batch_decode(ids_a, skip_special_tokens=False)[0]
                parsed_a = local_vision_processor.post_process_generation(raw_a, task=task_a, image_size=screenshot.size)
                region_data = parsed_a.get(task_a, {})
                if region_data and "labels" in region_data and "bboxes" in region_data:
                    for label, bbox in zip(region_data["labels"], region_data["bboxes"]):
                        label = label.strip()
                        if not label or len(label) > 80:
                            continue
                        if not any(c.isalpha() for c in label):
                            continue
                        cx = int((bbox[0] + bbox[2]) / 2)
                        cy = int((bbox[1] + bbox[3]) / 2)
                        if 0 <= cx <= sw and 0 <= cy <= sh:
                            region_labels.append(f"[{cx},{cy}] VISUAL: {label}")
            except Exception:
                pass
        except Exception as e:
            print(f"[⚠️ SCREEN_ANALYZE] Vision error: {e}")
        
        context["screen_text_blocks"] = text_blocks[:30]
        context["screen_region_labels"] = region_labels[:20]
        
        summary = []
        summary.append(f"Active window: {window_name}")
        if context.get("screen_is_ide"):
            summary.append(f"IDE detected: {context['screen_ide_type']}")
        if context.get("screen_is_browser"):
            summary.append("Browser window active")
            if context.get("screen_is_youtube"):
                summary.append("YouTube detected in browser")
        if text_blocks:
            summary.append(f"Visible text ({len(text_blocks)} items): " + ", ".join(text_blocks[:8]))
        if region_labels:
            summary.append(f"Visible regions ({len(region_labels)} items): " + ", ".join(region_labels[:5]))
        
        context["screen_summary"] = "\n".join(summary)
        print(f"[🔍 SCREEN_ANALYZE] Done. {len(text_blocks)} text blocks, {len(region_labels)} regions.")
        
        detect_screen_context._cache = context.copy()
        detect_screen_context._cache_time = now
        return context
    except Exception as e:
        print(f"[⚠️ SCREEN_ANALYZE] Error: {e}")
        context["screen_summary"] = f"Analysis error: {e}"
        context["screen_active_window"] = "unknown"
        context["screen_is_ide"] = False
        context["screen_is_browser"] = False
        context["screen_is_youtube"] = False
        context["screen_text_blocks"] = []
        context["screen_region_labels"] = []
        return context

def verify_window_exists(app_name):
    """Check if an application window is currently open and focused.
    Returns (exists, hwnd) tuple.
    """
    import win32gui
    import uiautomation as auto
    
    app_patterns = {
        "chrome": ["chrome", "google chrome"],
        "edge": ["edge", "microsoft edge"],
        "firefox": ["firefox", "mozilla"],
        "vscode": ["visual studio code", "code - ", "vscode"],
        "antigravity": ["antigravity"]
    }
    
    patterns = app_patterns.get(app_name.lower(), [app_name.lower()])
    
    try:
        all_windows = []
        def enum_windows(hwnd, results):
            if win32gui.IsWindowVisible(hwnd):
                window_title = win32gui.GetWindowText(hwnd)
                if window_title:
                    results.append((hwnd, window_title))
        
        win32gui.EnumWindows(enum_windows, all_windows)
        
        for hwnd, title in all_windows:
            title_lower = title.lower()
            if any(p in title_lower for p in patterns):
                # Check if window is minimized (needs to be restored)
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.3)
                return True, hwnd
        
        # Also check active window via uiautomation
        active = auto.GetForegroundControl()
        if active and active.Name:
            active_lower = active.Name.lower()
            if any(p in active_lower for p in patterns):
                return True, None
    except Exception as e:
        print(f"[🔍 WINDOW_CHECK] Error: {e}")
    
    return False, None

def is_browser_window_active():
    """Check if current active window is a browser."""
    import uiautomation as auto
    try:
        active = auto.GetForegroundControl()
        if active and active.Name:
            name = active.Name.lower()
            return any(b in name for b in ["chrome", "edge", "firefox", "brave"])
    except:
        pass
    return False

def is_youtube_window_active():
    """Check if current active window is YouTube."""
    import uiautomation as auto
    try:
        active = auto.GetForegroundControl()
        if active and active.Name:
            name = active.Name.lower()
            return "youtube" in name
    except:
        pass
    return False

def verify_address_bar_focused(timeout=1.0):
    """Verify that the address bar actually received focus after ctrl+L."""
    import uiautomation as auto
    try:
        screenshot = pyautogui.screenshot()
        text_blocks = vision_ocr(screenshot)
        
        # Look for typical address bar hints
        for block in text_blocks:
            label = block["desc"].lower()
            if any(x in label for x in ["https://", "http://", "www.", ".com", ".org", "chrome://"]):
                # Likely an address bar - check if it's editable
                pass
        
        # Also check via UIA - address bar should have focus
        active = auto.GetForegroundControl()
        if active:
            # Check if there's a focused edit control at top of screen
            for control, depth in auto.WalkControl(active, maxDepth=4):
                if control.ControlTypeName == "EditControl":
                    try:
                        rect = control.BoundingRectangle
                        if rect.Width() > 100 and rect.Height() > 20:
                            # Check if it's in the top portion
                            if rect.top < pyautogui.size()[1] * 0.2:
                                return True
                    except:
                        pass
        return False
    except Exception as e:
        print(f"[🔍 ADDRESS_CHECK] Error: {e}")
        return True  # Default to true on error to avoid blocking

def retry_failed_action(step_string, context, max_attempts=3):
    """Retry a failed action with alternative strategies."""
    if context.get("_retry_attempt", 0) >= max_attempts:
        print(f"[🔁 MAX RETRIES] Max retry attempts ({max_attempts}) reached for: {step_string}")
        return False, context
    
    context["_retry_attempt"] = context.get("_retry_attempt", 0) + 1
    attempt = context["_retry_attempt"]
    
    step_lower = step_string.lower()
    
    # Strategy for "click first video" - find actual video titles on screen
    if ("click" in step_lower and "video" in step_lower and attempt == 1):
        print(f"[🔁 RETRY {attempt}] Finding video titles on screen...")
        
        # Get all video candidates - thumbnails with titles
        screenshot = pyautogui.screenshot().convert("RGB")
        
        # First try: Find video thumbnails visually
        coords = find_video_thumbnail("video", broad=True)
        if coords:
            print(f"[🎯 RETRY] Clicking video thumbnail at ({coords[0]}, {coords[1]})")
            pyautogui.click(coords[0], coords[1])
            time.sleep(1.5)
            return True, context
        
        # Second try: Find text that looks like video titles (not channel names, URLs, etc.)
        text_blocks = vision_ocr(screenshot)
        
        video_title_keywords = ["video", "watch", "funny", "horror", "game", "tutorial", "music", 
                              "pewdiepie", "markiplier", "jacksepticeye", "ninja", "shroud",
                              "minecraft", "fortnite", "valorant", "call of duty", "cooking",
                              "vlog", "review", "reaction", "compilation", "best", "top", "epic"]
        
        skip_patterns = ["https", "http", "www", ".com", ".org", "chrome", "edge", "firefox",
                        "youtube", "home", "trending", "subscriptions", "library", "history",
                        "settings", "profile", "avatar", "sign in", "subscribe", "notification"]
        
        candidates = []
        for block in text_blocks:
            label = block["desc"].lower().strip()
            # Skip very short or very long text
            if len(label) < 3 or len(label) > 50:
                continue
            # Skip URLs and UI elements
            if any(sp in label for sp in skip_patterns):
                continue
            # Look for text that could be video titles
            if any(vt in label for vt in video_title_keywords) or (len(label.split()) >= 2 and len(label) > 5):
                # Additional check: avoid channel/profile names
                if not any(p in label for p in ["subscriber", "views", "channel", "upload", "new"]):
                    candidates.append((block["cx"], block["cy"], block["desc"]))
        
        if candidates:
            # Click the first reasonable candidate
            cx, cy, desc = candidates[0]
            print(f"[🎯 RETRY] Clicking potential video title: '{desc}' at ({cx}, {cy})")
            pyautogui.click(cx, cy)
            time.sleep(1.5)
            return True, context
        
        print(f"[⚠️ RETRY] No video candidates found on screen")
    
    # Strategy 1: For BROWSER_ACTION focus_address, try clicking address bar visually
    if ("focus_address" in step_lower or ("hotkey ctrl l" in step_lower)) and not is_browser_window_active():
        print(f"[🔁 RETRY {attempt}] Trying visual address bar click as alternative...")
        coords = smart_find_element("address_bar")
        if coords:
            pyautogui.click(coords[0], coords[1])
            time.sleep(0.5)
            return True, context
        # Try to launch browser first if not active
        if not is_browser_window_active():
            print(f"[🔁 RETRY {attempt}] Browser not detected, launching Chrome...")
            success, context = execute_macro_step("LAUNCH chrome", context)
            if success:
                time.sleep(1.0)
                success2, context = execute_macro_step("SELECT_PROFILE", context)
                return success2, context
    
    # Strategy 3: For TYPE_IN address_bar, try visual click + type
    if "type_in" in step_lower and "address_bar" in step_lower:
        print(f"[🔁 RETRY {attempt}] Trying visual address bar click + type...")
        coords = smart_find_element("address_bar")
        if coords:
            pyautogui.click(coords[0], coords[1])
            time.sleep(0.3)
            # Parse the text to type from the step
            parts = step_string.split()
            if len(parts) >= 2:
                text = " ".join(parts[1:]).strip('"').strip("'")
                pyautogui.hotkey('ctrl', 'a')
                time.sleep(0.1)
                pyautogui.press('backspace')
                time.sleep(0.1)
                pyautogui.write(text, interval=0.03)
                return True, context
    
    context["_retry_attempt"] = 0  # Reset for next action
    return False, context

def verify_state_change(expected_change=None, tolerance=5):
    global _previous_screenshot
    try:
        current = pyautogui.screenshot()
        
        if _previous_screenshot is None:
            _previous_screenshot = current
            return True  # First verification, always pass
        
        # Quick pixel-level comparison of center region
        w, h = current.size
        center_box = (w//4, h//4, 3*w//4, 3*h//4)
        
        prev_center = _previous_screenshot.crop(center_box)
        curr_center = current.crop(center_box)
        
        # Convert to numpy for comparison
        prev_arr = np.array(prev_center)
        curr_arr = np.array(curr_center)
        
        # Calculate difference
        diff = np.abs(prev_arr.astype(int) - curr_arr.astype(int))
        changed_pixels = np.sum(diff > tolerance)
        total_pixels = diff.size // 3
        
        change_ratio = changed_pixels / total_pixels if total_pixels > 0 else 0
        
        _previous_screenshot = current
        
        if change_ratio > 0.01:  # At least 1% of pixels changed
            print(f"[✅ VERIFY] State confirmed changed ({change_ratio:.2%} pixels)")
            return True
        else:
            print(f"[⚠️ VERIFY] No significant change detected ({change_ratio:.4%} pixels)")
            return False

    except Exception as e:
        print(f"[!] State verification error: {e}")
        _previous_screenshot = pyautogui.screenshot()
        return True  # Default to True on error to avoid blocking

def _detect_ide_window():
    """Detect if VS Code or Antigravity IDE window is currently focused"""
    try:
        import uiautomation as auto
        active_window = auto.GetForegroundControl()
        if not active_window:
            return None
        window_name = active_window.Name.lower() if active_window.Name else ""
        if "visual studio code" in window_name or "code - " in window_name or "vscode" in window_name:
            return "vscode"
        if "antigravity" in window_name:
            return "antigravity"
    except:
        pass
    return None

def _find_code_editor_area():
    """Find the main code editor area coordinates using UI automation"""
    try:
        import uiautomation as auto
        active_window = auto.GetForegroundControl()
        if not active_window:
            return None
        
        for control, depth in auto.WalkControl(active_window, maxDepth=6):
            control_type = control.ControlTypeName.lower() if control.ControlTypeName else ""
            class_name = control.ClassName.lower() if control.ClassName else ""
            
            if control_type in ["editcontrol", "document"] or "editor" in class_name or "code" in class_name:
                rect = control.BoundingRectangle
                if rect.Width() > 100 and rect.Height() > 100:
                    center_x = rect.left + rect.Width() // 2
                    center_y = rect.top + rect.Height() // 2
                    screen_w, screen_h = pyautogui.size()
                    if 0 <= center_x <= screen_w and 0 <= center_y <= screen_h:
                        return (center_x, center_y)
        
        screen_w, screen_h = pyautogui.size()
        return (screen_w // 2 + 100, screen_h // 2)
    except Exception as e:
        print(f"[!] _find_code_editor_area error: {e}")
        return None

def _create_project_vscode(project_path, project_name, context):
    """Create project folder structure in VS Code via UI interactions"""
    try:
        full_path = os.path.abspath(project_path)
        os.makedirs(full_path, exist_ok=True)
        print(f"[📁 CREATE_PROJECT] Folder created: {full_path}")
        
        coords = smart_find_element("explorer")
        if coords:
            pyautogui.click(coords[0], coords[1])
            time.sleep(1.0)
        
        screen_w, screen_h = pyautogui.size()
        explorer_area_x = screen_w // 4
        explorer_area_y = screen_h // 3
        
        pyautogui.click(explorer_area_x, explorer_area_y)
        time.sleep(0.5)
        pyautogui.right_click()
        time.sleep(0.5)
        
        pyautogui.press('down')
        time.sleep(0.2)
        pyautogui.press('down')
        time.sleep(0.2)
        pyautogui.press('enter')
        time.sleep(0.5)
        
        pyautogui.write(project_name)
        time.sleep(0.2)
        pyautogui.press('enter')
        time.sleep(0.5)
        
        context["last_action"] = "CREATE_PROJECT"
        return True, context
    except Exception as e:
        print(f"[❌ CREATE_PROJECT] {e}")
        context["last_error"] = str(e)
        return False, context

def _create_project_antigravity(project_path, project_name, context):
    """Create project folder structure in Antigravity IDE and open it."""
    try:
        full_path = os.path.abspath(project_path)
        os.makedirs(full_path, exist_ok=True)
        print(f"[📁 CREATE_PROJECT] Folder created: {full_path}")
        set_status_text(f"Opening Antigravity\n{full_path}")
        
        # Check if Antigravity is already the active window
        import uiautomation as auto
        active_window = auto.GetForegroundControl()
        antigravity_active = False
        if active_window:
            window_name = active_window.Name.lower() if active_window.Name else ""
            if "antigravity" in window_name:
                antigravity_active = True
        
        # Only launch if Antigravity is NOT already active
        if not antigravity_active:
            print("[*] Launching Antigravity...")
            pyautogui.press('win')
            time.sleep(0.4)
            pyautogui.write("Antigravity", interval=0.03)
            time.sleep(0.5)
            pyautogui.press('enter')
            time.sleep(1.5)
        else:
            print("[*] Antigravity already active, reusing existing window")
        
        print(f"[📂 OPEN FOLDER] Opening in Antigravity: {full_path}")
        set_status_text(f"Opening folder in Antigravity\n{full_path}")
        
        time.sleep(0.5)
        
        pyautogui.hotkey('alt', 'f')
        time.sleep(0.3)
        
        pyautogui.press('o')
        time.sleep(0.3)
        
        pyautogui.write(full_path, interval=0.03)
        time.sleep(0.2)
        pyautogui.press('enter')
        time.sleep(0.5)
        
        context["last_action"] = "CREATE_PROJECT"
        context["last_error"] = None
        return True, context
    except Exception as e:
        print(f"[❌ CREATE_PROJECT] {e}")
        context["last_error"] = str(e)
        return False, context

def _get_screen_hash():
    """Get a simple hash of the current screen to detect if scrolling changed anything."""
    try:
        screenshot = pyautogui.screenshot().convert("L")
        return screenshot.tobytes()[:10000]
    except Exception:
        return None


def click_first_search_result(context=None, search_query=None):
    """Click the first real search result on the screen after a search query.
    
    If no results are found on the current view, scrolls down and retries
    until a result is found or the page cannot scroll anymore.
    
    Filters out:
    - URLs/address bar text
    - Browser chrome/UI text
    - IDE/menu bar text
    - Black/grey plain-text noise
    - Very short or very long labels
    - Navigation/filter labels
    - Antigravity/IDE internal markup/coordinates
    - The search query text itself (to avoid clicking the search input)
    """
    if context is None:
        context = {}
    
    print("[🔍 SEARCH RESULT] Looking for first clickable search result...")
    
    try:
        # Verify we're actually in a browser before clicking search results
        detect_screen_context(context)
        is_browser = context.get("screen_is_browser", False)
        active_window = context.get("screen_active_window", "")
        
        if not is_browser or not any(browser in active_window.lower() for browser in ["chrome", "edge", "firefox", "brave"]):
            print(f"[⚠️ SEARCH RESULT] Not in a browser (current: {active_window}). Aborting click to avoid UI chrome.")
            return False, context
        
        max_scroll_attempts = 8
        scroll_amount = 3
        
        for attempt in range(max_scroll_attempts):
            screenshot = pyautogui.screenshot().convert("RGB")
            sw, sh = screenshot.size
            blocks = vision_ocr(screenshot)
            
            skip_patterns = [
                "google", "search", "url", "http", "https", "www.", ".com", ".org", ".net",
                "chrome", "edge", "firefox", "browser", "address", "tab", "bookmark",
                "history", "settings", "menu", "profile", "sign in", "login", "gmail",
                "images", "videos", "maps", "news", "shopping", "youtube", "facebook",
                "twitter", "instagram", "linkedin", "github", "stack", "overflow",
                "skip to", "sign out", "download", "extension", "app", "store",
                "file", "edit", "view", "run", "terminal", "help", "window", "debug",
                "format", "tools", "git", "refactor", "navigation", "preferences",
                "new", "open", "save", "close", "exit", "undo", "redo", "cut", "copy",
                "paste", "find", "replace", "select all", "zoom", "fullscreen", "full screen"
            ]
            
            ui_chrome_keywords = {
                "file", "edit", "view", "run", "terminal", "help", "window", "debug",
                "format", "tools", "git", "refactor", "navigation", "preferences",
                "new", "open", "save", "close", "exit", "undo", "redo", "cut", "copy",
                "paste", "find", "replace", "select all", "zoom", "fullscreen", "full screen",
                "chrome", "firefox", "edge", "browser", "address", "tab", "bookmark",
                "history", "settings", "menu", "profile", "sign in", "login", "gmail",
                "images", "videos", "maps", "news", "shopping", "youtube", "facebook",
                "twitter", "instagram", "linkedin", "github", "stack", "overflow",
                "skip to", "sign out", "download", "extension", "app", "store",
                "all", "none", "cancel", "ok", "apply", "reset", "clear", "refresh"
            }
            
            candidates = []
            for block in blocks:
                label = block["desc"].strip()
                label_lower = label.lower()
                
                if len(label) < 8 or len(label) > 120:
                    continue
                
                # Skip Antigravity/IDE internal markup like <loc_999><loc_549>...X
                if "<loc_" in label_lower or label_lower.count("<") > 2 or label_lower.count(">") > 2:
                    continue
                
                # Skip anything that looks like coordinate/markup data
                if any(pattern in label_lower for pattern in ["<loc_", ">x<", "><", "loc_999", "loc_549"]):
                    continue
                
                # Skip the search query text itself - avoid clicking the search input
                if search_query and search_query.lower() in label_lower:
                    continue
                
                # Skip text that is very similar to the search query (fuzzy match)
                if search_query and len(search_query) > 5:
                    query_words = set(search_query.lower().split())
                    label_words = set(label_lower.split())
                    if len(query_words) > 0:
                        overlap = len(query_words & label_words) / len(query_words)
                        if overlap > 0.8:
                            continue
                
                # Skip text in the top portion of the screen (address bar / search box area)
                cy = block.get("cy", 0)
                if cy < sh * 0.15:
                    continue
                
                if any(skip in label_lower for skip in skip_patterns):
                    continue
                
                if label_lower in ["back", "next", "close", "menu", "settings", "search", "enter", "cancel", "ok"]:
                    continue
                
                words = label_lower.split()
                ui_word_count = sum(1 for w in words if w in ui_chrome_keywords)
                if len(words) > 0 and ui_word_count / len(words) > 0.6:
                    continue
                
                if label_lower.startswith("/") or label_lower.startswith(">"):
                    continue
                
                if "</" in label_lower or "/>" in label_lower:
                    continue
                
                score = len(label)
                if any(c.isupper() for c in label):
                    score += 5
                if " " in label:
                    score += 3
                
                candidates.append((block["cx"], block["cy"], score, label))
            
            if candidates:
                candidates.sort(key=lambda x: x[2], reverse=True)
                best = candidates[0]
                print(f"[🎯 SEARCH RESULT] Clicking best candidate: '{best[3]}' at ({best[0]}, {best[1]})")
                
                pyautogui.moveTo(best[0], best[1], duration=0.3)
                time.sleep(0.1)
                pyautogui.click()
                time.sleep(1.0)
                
                context["last_action"] = "CLICK first search result"
                return True, context
            
            if attempt < max_scroll_attempts - 1:
                before_hash = _get_screen_hash()
                pyautogui.scroll(-scroll_amount)
                time.sleep(0.8)
                after_hash = _get_screen_hash()
                
                if before_hash is not None and before_hash == after_hash:
                    print("[ℹ️ SEARCH RESULT] Page cannot scroll further. No results found.")
                    break
                
                print(f"[🔄 SEARCH RESULT] No results on screen. Scrolling down... (attempt {attempt + 1}/{max_scroll_attempts})")
        
        print("[!] No valid search result candidates found after scrolling")
        return False, context
    except Exception as e:
        print(f"[!] click_first_search_result error: {e}")
        context["last_error"] = str(e)
        return False, context
        
        skip_patterns = [
            "google", "search", "url", "http", "https", "www.", ".com", ".org", ".net",
            "chrome", "edge", "firefox", "browser", "address", "tab", "bookmark",
            "history", "settings", "menu", "profile", "sign in", "login", "gmail",
            "images", "videos", "maps", "news", "shopping", "youtube", "facebook",
            "twitter", "instagram", "linkedin", "github", "stack", "overflow",
            "skip to", "sign out", "download", "extension", "app", "store",
            # IDE/menu bar text
            "file", "edit", "view", "run", "terminal", "help", "window", "debug",
            "format", "tools", "git", "refactor", "navigation", "preferences",
            "new", "open", "save", "close", "exit", "undo", "redo", "cut", "copy",
            "paste", "find", "replace", "select all", "zoom", "fullscreen", "full screen"
        ]
        
        # Additional hard filters for UI chrome
        ui_chrome_keywords = {
            "file", "edit", "view", "run", "terminal", "help", "window", "debug",
            "format", "tools", "git", "refactor", "navigation", "preferences",
            "new", "open", "save", "close", "exit", "undo", "redo", "cut", "copy",
            "paste", "find", "replace", "select all", "zoom", "fullscreen", "full screen",
            "chrome", "firefox", "edge", "browser", "address", "tab", "bookmark",
            "history", "settings", "menu", "profile", "sign in", "login", "gmail",
            "images", "videos", "maps", "news", "shopping", "youtube", "facebook",
            "twitter", "instagram", "linkedin", "github", "stack", "overflow",
            "skip to", "sign out", "download", "extension", "app", "store",
            "all", "none", "cancel", "ok", "apply", "reset", "clear", "refresh"
        }
        
        candidates = []
        for block in blocks:
            label = block["desc"].strip()
            label_lower = label.lower()
            
            # Skip very short or very long text
            if len(label) < 8 or len(label) > 120:
                continue
            
            # Skip UI noise
            if any(skip in label_lower for skip in skip_patterns):
                continue
            
            # Skip anything that looks like button/UI text
            if label_lower in ["back", "next", "close", "menu", "settings", "search", "enter", "cancel", "ok"]:
                continue
            
            # Skip pure menu/UI chrome sequences
            words = label_lower.split()
            ui_word_count = sum(1 for w in words if w in ui_chrome_keywords)
            if len(words) > 0 and ui_word_count / len(words) > 0.6:
                continue
            
            # Skip anything that looks like a URL path fragment
            if label_lower.startswith("/") or label_lower.startswith(">"):
                continue
            
            # Skip anything that looks like HTML/XML tags
            if "</" in label_lower or "/>" in label_lower:
                continue
            
            # Prefer longer, descriptive-looking labels (likely result titles)
            score = len(label)
            if any(c.isupper() for c in label):
                score += 5  # Title case likely means a real result title
            if " " in label:
                score += 3  # Multi-word more likely a real result
            
            candidates.append((block["cx"], block["cy"], score, label))
        
        if not candidates:
            print("[!] No valid search result candidates found")
            return False, context
        
        candidates.sort(key=lambda x: x[2], reverse=True)
        best = candidates[0]
        print(f"[🎯 SEARCH RESULT] Clicking best candidate: '{best[3]}' at ({best[0]}, {best[1]})")
        
        pyautogui.moveTo(best[0], best[1], duration=0.3)
        time.sleep(0.1)
        pyautogui.click()
        time.sleep(1.0)
        
        context["last_action"] = "CLICK first search result"
        return True, context
    except Exception as e:
        print(f"[!] click_first_search_result error: {e}")
        context["last_error"] = str(e)
        return False, context

def smart_click(target_label, context=None, click_type="left"):
    """Enhanced click with multiple strategies, confidence scoring, and verification."""
    if context is None:
        context = {}
    
    print(f"[🎯 SMART CLICK] Attempting to click: {target_label} (type: {click_type})")
    
    def _do_click(x, y):
        pyautogui.moveTo(x, y, duration=0.3)
        time.sleep(0.1)
        if click_type == "right":
            pyautogui.rightClick()
        elif click_type == "double":
            pyautogui.doubleClick()
        else:
            pyautogui.click()
    
    best_coords = None
    best_confidence = 0
    
    coords = smart_find_element(target_label)
    if coords:
        best_coords = coords
        best_confidence = 0.9
        print(f"[🎯 SMART CLICK] Found coordinates: {best_coords}")
        _do_click(best_coords[0], best_coords[1])
    else:
        alt_labels = _get_alternative_labels(target_label)
        for alt in alt_labels[:3]:
            coords = smart_find_element(alt)
            if coords:
                best_coords = coords
                best_confidence = 0.7
                print(f"[🎯 SMART CLICK] Found alternative '{alt}': {best_coords}")
                context["last_successful_label"] = alt
                _do_click(best_coords[0], best_coords[1])
                break
    
    if best_coords and best_coords[0] is not None and best_coords[1] is not None and best_confidence > 0.5:
        if target_label in _expectations:
            if check_expectation(target_label):
                clear_expectation(target_label)
                return True, context
        
        if verify_state_change():
            return True, context
    
    direct_match = _try_direct_ui_automation(target_label)
    if direct_match and direct_match[0] is not None:
        print(f"[🎯 SMART CLICK] Direct UI Automation match: {direct_match}")
        _do_click(direct_match[0], direct_match[1])
        return True, context
    
    # YouTube thumbnail fallback: find text via OCR and click on it
    yt_keywords = ["youtube", "video", "watch", "thumbnail", "clip", "channel", "markplier", "markiplier", "tutorial", "game", "music", "funny", "horror"]
    target_lower = target_label.lower().strip()
    
    # Handle "CLICK video" or "CLICK video thumbnail" - find first video on screen
    is_browser = context.get("screen_is_browser", False)
    if target_lower in ["video", "video thumbnail", "thumbnail"] or (is_browser and "youtube" in context.get("screen_active_window", "").lower()):
        print(f"[🎬 YOUTUBE FALLBACK] Finding videos on YouTube screen...")
        try:
            # Try to find video thumbnails first
            video_result = find_video_thumbnail(f"{target_label} video", broad=True)
            if video_result:
                print(f"[🎬 YOUTUBE FALLBACK] Found video thumbnail at ({video_result[0]}, {video_result[1]})")
                _do_click(video_result[0], video_result[1])
                return True, context
            
            # Fallback: Find text that looks like video titles
            screenshot = pyautogui.screenshot().convert("RGB")
            text_blocks = vision_ocr(screenshot)
            
            # Filter for video title candidates
            candidates = []
            for block in text_blocks:
                label_lower = block["desc"].lower()
                # Skip UI elements and URLs
                if any(x in label_lower for x in ["https", "http", "www.", ".com", "chrome", "edge", "firefox", 
                                                   "youtube", "home", "trending", "subscriptions", "library",
                                                   "search", "filter", "settings", "apps", "signin", "subscribe"]):
                    continue
                # Look for video title patterns
                if len(label_lower) > 5 and len(label_lower) < 60:
                    if any(kw in label_lower for kw in ["markiplier", "pewdiepie", "funny", "horror", "game", 
                                                          "tutorial", "music", "vlog", "review", "epic", "best", "top"]):
                        candidates.append((block["cx"], block["cy"], block["desc"]))
            
            if candidates:
                cx, cy, desc = candidates[0]
                print(f"[🎬 YOUTUBE FALLBACK] Clicking video title: '{desc}' at ({cx}, {cy})")
                _do_click(cx, cy)
                return True, context
                
        except Exception as e:
            print(f"[!] YouTube fallback error: {e}")
    
    context["last_error"] = f"smart_click failed for: {target_label}"
    return False, context

def _get_alternative_labels(target):
    """Generate alternative labels for an element."""
    target_lower = target.lower().strip()
    alternatives = []
    
    # Common synonyms
    synonyms = {
        "profile": ["account", "user", "avatar", "person", "face", "signin", "login"],
        "account": ["profile", "user", "avatar", "person"],
        "settings": ["preferences", "options", "config", "gear", "cog"],
        "search": ["find", "lookup", "query", "search box", "search bar"],
        "menu": ["hamburger", "more", "options", "three dots", "submenu"],
        "close": ["x", "exit", "dismiss", "cancel"],
        "save": ["floppy", "disk", "save as"],
        "new": ["create", "add", "fresh", "new file", "new document"],
        "delete": ["remove", "trash", "bin", "delete file"],
        "edit": ["modify", "change", "alter", "rename"],
        "help": ["support", "assistance", "question", "help center"],
        "home": ["main", "start", "beginning", "homepage"],
        "back": ["previous", "return", "undo", "go back"],
        "next": ["forward", "continue", "proceed", "skip"],
        "download": ["save", "export", "download file"],
        "upload": ["import", "send", "upload file"],
        "full screen": ["fullscreen", "fullscreen button", "expand", " maximize "],
        "fullscreen": ["full screen", "expand", "maximize"],
        "video": ["thumbnail", "youtube", "play", "movie"],
        "youtube": ["video", "watch", "play"],
        "watch": ["play", "video", "thumbnail", "youtube"],
        "address bar": ["url", "omnibox", "search bar"],
        "avatar": ["profile", "user", "account", "person"],
        "terminal": ["console", "shell", "cmd", "powershell"],
        "explorer": ["file explorer", "sidebar", "files", "folder"],
    }
    
    for key, syns in synonyms.items():
        if key in target_lower:
            alternatives.extend(syns)
        elif any(s in target_lower for s in syns):
            alternatives.append(key)
    
    # Add partial matches
    for key in synonyms:
        if key in target_lower:
            for word in target_lower.split():
                if word != key and len(word) > 3:
                    alternatives.append(word)
    
    return list(set(alternatives))[:5]  # Max 5 alternatives

def _try_direct_ui_automation(target_label):
    """Direct UI Automation attempt with broad search and confidence scoring."""
    try:
        import uiautomation as auto
        active_window = auto.GetForegroundControl()
        if not active_window:
            return None, None
        
        results = []
        for control, depth in auto.WalkControl(active_window, maxDepth=6):
            name = control.Name.lower() if control.Name else ""
            auto_id = control.AutomationId.lower() if control.AutomationId else ""
            
            if target_label.lower() in name or target_label.lower() in auto_id:
                rect = control.BoundingRectangle
                if rect.Width() > 0 and rect.Height() > 0:
                    cx = rect.left + rect.Width() // 2
                    cy = rect.top + rect.Height() // 2
                    confidence = 0.85 - (depth * 0.05)
                    results.append((cx, cy, confidence, control.Name))
        
        if results:
            results.sort(key=lambda r: r[2], reverse=True)
            return results[0][0], results[0][1]
        
        target_lower = target_label.lower().strip()
        if any(kw in target_lower for kw in PROFILE_KEYWORDS):
            for control, depth in auto.WalkControl(active_window, maxDepth=6):
                if "image" in control.ControlTypeName.lower():
                    rect = control.BoundingRectangle
                    if rect.Width() > 20 and rect.Height() > 20:
                        screen_w, screen_h = pyautogui.size()
                        center_x = rect.left + rect.Width() // 2
                        center_y = rect.top + rect.Height() // 2
                        if center_y < screen_h * 0.3:
                            return center_x, center_y
    except Exception:
        pass
    return None, None

def launch_application(app_name):
    """Natively starts common system apps or performs Windows keyboard-based search launcher."""
    app_name_lower = app_name.lower().strip()
    
    # Special handling for Chrome/Edge: focus if already open, DON'T launch a second instance
    if app_name_lower in ["chrome", "google chrome"]:
        try:
            import win32gui
            import win32con
            
            def find_chrome_window(hwnd, results):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if "chrome" in title.lower() or "google chrome" in title.lower():
                        results.append(hwnd)
            
            chrome_windows = []
            win32gui.EnumWindows(find_chrome_window, chrome_windows)
            
            if chrome_windows:
                # Chrome already open - just focus it, skip launch entirely
                print(f"[🎯 LAUNCH] Chrome already running ({len(chrome_windows)} windows) - focusing existing window")
                try:
                    win32gui.SetForegroundWindow(chrome_windows[0])
                    time.sleep(0.3)
                    return True
                except Exception as e:
                    print(f"[!] Focus failed: {e}, trying Alt+Tab...")
                    pyautogui.hotkey('alt', 'tab')
                    time.sleep(0.3)
                    return True
        except Exception as e:
            print(f"[!] Chrome check error: {e}")
        
        # Only reach here if Chrome is NOT running at all
        print(f"[🚀 WIN SEARCH] Opening Chrome via Windows search (no existing window found)")
        pyautogui.press('win')
        time.sleep(0.4)
        pyautogui.write("chrome", interval=0.03)
        time.sleep(0.5)
        pyautogui.press('enter')
        time.sleep(2.0)
        return True
    
    if app_name_lower in ["edge", "microsoft edge", "msedge"]:
        try:
            import win32gui
            
            def find_edge_window(hwnd, results):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if "microsoft edge" in title.lower():
                        results.append(hwnd)
            
            edge_windows = []
            win32gui.EnumWindows(find_edge_window, edge_windows)
            
            if edge_windows:
                print(f"[🎯 LAUNCH] Edge already running - focusing existing window")
                try:
                    win32gui.SetForegroundWindow(edge_windows[0])
                    time.sleep(0.3)
                    return True
                except Exception as e:
                    print(f"[!] Focus failed: {e}, trying Alt+Tab...")
                    pyautogui.hotkey('alt', 'tab')
                    time.sleep(0.3)
                    return True
        except Exception as e:
            print(f"[!] Edge check error: {e}")
        
        print(f"[🚀 WIN SEARCH] Opening Edge via Windows search")
        pyautogui.press('win')
        time.sleep(0.4)
        pyautogui.write("edge", interval=0.03)
        time.sleep(0.5)
        pyautogui.press('enter')
        time.sleep(2.0)
        return True
    
    # For other apps, use Windows search
    print(f"[🚀 WIN SEARCH LAUNCH] Searching and launching: {app_name}")
    pyautogui.press('win')
    time.sleep(0.4)
    pyautogui.write(app_name, interval=0.03)
    time.sleep(0.5)
    pyautogui.press('enter')
    time.sleep(1.5)
    return True

def focus_window_by_title(title_fragment):
    """Focus a specific window by part of its title."""
    try:
        import win32gui
        
        def enum_windows(hwnd, results):
            if win32gui.IsWindowVisible(hwnd):
                window_title = win32gui.GetWindowText(hwnd)
                if window_title:
                    results.append((hwnd, window_title))
        
        windows = []
        win32gui.EnumWindows(enum_windows, windows)
        target_lower = title_fragment.lower()
        
        for hwnd, name in windows:
            if target_lower in name.lower():
                try:
                    win32gui.SetForegroundWindow(hwnd)
                    print(f"[🎯 FOCUS] Focused window: {name}")
                    time.sleep(0.5)
                    return True
                except Exception as e:
                    print(f"[!] SetForegroundWindow failed: {e}, trying SetFocus...")
                    try:
                        import uiautomation as auto
                        control = auto.ControlFromHandle(hwnd)
                        if control:
                            control.SetFocus()
                            time.sleep(0.5)
                            return True
                    except:
                        pass
        print(f"[❌ FOCUS] Window not found: {title_fragment}")
        return False
    except Exception as e:
        print(f"[❌ FOCUS] Error: {e}")
        return False

def _select_chrome_profile(context=None):
    """Detects Chrome's profile picker screen and selects the first user profile."""
    try:
        import uiautomation as auto
        import pyautogui
        
        # Search ALL top-level windows for Chrome profile picker
        profile_picker_window = None
        try:
            all_windows = []
            def enum_windows(hwnd, results):
                if win32gui.IsWindowVisible(hwnd):
                    window_title = win32gui.GetWindowText(hwnd)
                    if window_title:
                        results.append((hwnd, window_title))
            win32gui.EnumWindows(enum_windows, all_windows)
            
            for hwnd, window_name in all_windows:
                window_name_lower = window_name.lower()
                if "chrome" in window_name_lower or "who's using" in window_name_lower:
                    has_guest = False
                    has_add = False
                    try:
                        window = auto.ControlFromHandle(hwnd)
                        if window:
                            for control, depth in auto.WalkControl(window, maxDepth=3):
                                name_lower = control.Name.lower() if control.Name else ""
                                if "guest" in name_lower:
                                    has_guest = True
                                if "add profile" in name_lower or "add" in name_lower:
                                    has_add = True
                    except:
                        pass
                    
                    if has_guest or has_add:
                        profile_picker_window = hwnd
                        print(f"[👤 SELECT_PROFILE] Found profile picker window: {window_name}")
                        break
        except Exception as e:
            print(f"[👤 SELECT_PROFILE] Window scan error: {e}")
        
        if not profile_picker_window:
            active_window = auto.GetForegroundControl()
            if active_window:
                window_name = active_window.Name.lower() if active_window.Name else ""
                if "chrome" in window_name or "who's using" in window_name:
                    profile_picker_window = active_window
        
        if not profile_picker_window:
            print("[👤 SELECT_PROFILE] No Chrome profile picker detected")
            return True
        
        if isinstance(profile_picker_window, int):
            print(f"[👤 SELECT_PROFILE] Chrome profile picker detected (hwnd: {profile_picker_window}), selecting first profile...")
            picker_control = auto.ControlFromHandle(profile_picker_window)
        else:
            print(f"[👤 SELECT_PROFILE] Chrome profile picker detected (window: {profile_picker_window.Name}), selecting first profile...")
            picker_control = profile_picker_window
        
        if not picker_control:
            print("[👤 SELECT_PROFILE] Could not get control from handle, trying keyboard fallback...")
            pyautogui.press('tab')
            time.sleep(0.3)
            pyautogui.press('enter')
            time.sleep(1.0)
            return True
        
        profile_cards = []
        try:
            for control, depth in auto.WalkControl(picker_control, maxDepth=5):
                if control.ControlTypeName in ["ButtonControl", "ListItemControl"]:
                    name = control.Name.lower() if control.Name else ""
                    skip_words = ["guest", "add profile", "add", "manage", "close", "minimize", "maximize", 
                                  "help", "settings", "feedback", "use without account", "sign out", "remove"]
                    if not any(sw in name for sw in skip_words):
                        try:
                            rect = control.BoundingRectangle
                            if rect.Width() > 0 and rect.Height() > 0:
                                profile_cards.append(control)
                        except Exception:
                            continue
        except Exception as e:
            print(f"[👤 SELECT_PROFILE] Error walking controls: {e}")
        
        if not profile_cards:
            print("[👤 SELECT_PROFILE] No profile cards found, trying keyboard navigation...")
            try:
                if isinstance(profile_picker_window, int):
                    win32gui.SetForegroundWindow(profile_picker_window)
                elif hasattr(profile_picker_window, 'SetFocus'):
                    profile_picker_window.SetFocus()
                time.sleep(0.3)
            except Exception as e:
                print(f"[👤 SELECT_PROFILE] Focus error: {e}")
            pyautogui.press('tab')
            time.sleep(0.3)
            pyautogui.press('enter')
            time.sleep(1.0)
            return True
        
        first_profile = profile_cards[0]
        try:
            rect = first_profile.BoundingRectangle
            if rect.Width() > 0 and rect.Height() > 0:
                center_x = rect.left + rect.Width() // 2
                center_y = rect.top + rect.Height() // 2
                print(f"[👤 SELECT_PROFILE] Clicking profile: '{first_profile.Name}' at ({center_x}, {center_y})")
                pyautogui.moveTo(center_x, center_y, duration=0.3)
                time.sleep(0.1)
                pyautogui.click()
                time.sleep(1.5)
                return True
        except Exception:
            pass
        
        print("[👤 SELECT_PROFILE] Could not get profile bounds, falling back to keyboard...")
        pyautogui.press('tab')
        time.sleep(0.3)
        pyautogui.press('enter')
        time.sleep(1.0)
        return True
        
    except Exception as e:
        print(f"[👤 SELECT_PROFILE] Error: {e}")
        return False

def execute_macro_step(step_string, context=None):
    """Executes a single structural instruction step with universal OS-level app routing."""
    if context is None:
        context = {"last_error": None, "retry_count": 0, "last_action": None, "workspace": os.getcwd()}
    
    parts = step_string.strip().split(" ", 1)
    command = parts[0].upper()
    argument = parts[1] if len(parts) > 1 else ""
    
    if command == "CREATE":
        arg_lower = argument.lower()
        if "project" in arg_lower or "new" in arg_lower:
            return execute_macro_step(f"CREATE_PROJECT {argument}", context)
        if "file" in arg_lower:
            cleaned = argument.replace("file", "", 1).replace("new", "", 1).strip()
            return execute_macro_step(f"CREATE_FILE {cleaned}", context)
        context["last_error"] = "Ambiguous CREATE. Use CREATE_PROJECT or CREATE_FILE."
        return False, context
    
# Get current prompt from context (set by main loop) or use step_string
    current_prompt = context.get("current_prompt", step_string)

    if command == "PRESS":
        key = argument.lower().strip("'").strip('"')
        if not key:
            print(f"[❌ PRESS] Missing key argument — step was '{step_string}'. Skipping empty PRESS.")
            context["last_error"] = "PRESS command has no key argument"
            return False, context
        print(f"[⌨️ KEY] Pressing: {key}")
        reason_overlay(f"PRESS {key} - executing keyboard action")
        pyautogui.press(key)
        context["last_action"] = step_string
        return True, context

    if command == "HOTKEY":
        keys = argument.lower().strip("'").strip('"').split()
        if not keys:
            print(f"[❌ HOTKEY] Missing keys argument — step was '{step_string}'. Skipping empty HOTKEY.")
            context["last_error"] = "HOTKEY command has no keys argument"
            return False, context
        print(f"[⌨️ HOTKEY] Triggering: {', '.join(keys)}")
        reason_overlay(f"HOTKEY {' + '.join(keys)} - executing hotkey combination")
        pyautogui.hotkey(*keys)
        context["last_action"] = step_string
        return True, context

    if command == "TYPE":
        payload = argument.strip().strip("'").strip('"')
        if not payload:
            print(f"[❌ TYPE] Missing text argument — step was '{step_string}'. Skipping empty TYPE.")
            context["last_error"] = "TYPE command has no text argument"
            return False, context
        print(f"[⌨️ TYPE] Typing: '{payload}'")
        reason_overlay(f"TYPE '{payload}' - entering text at current cursor position")
        pyautogui.write(payload, interval=0.03)
        context["last_action"] = step_string
        return True, context

    if command == "WAIT":
        try:
            seconds = float(argument.strip().strip("'").strip('"'))
        except ValueError:
            seconds = 2.0
        skip = get_skip_waits() or context.get("skip_waits", False)
        if skip:
            print(f"[🕒 WAIT] Skipped (skip_waits=True) - would have waited {seconds} seconds")
        else:
            print(f"[🕒 WAIT] Waiting {seconds} seconds...")
            time.sleep(seconds)
            reason_overlay(f"WAIT {seconds}s completed - proceeding with next action")
        context["last_action"] = step_string
        return True, context

    if command == "WAITING":
        arg_lower = argument.lower().strip()
        if arg_lower in ["false", "off", "0"]:
            set_skip_waits(True)
            print("[⚡ WAITING] Disabled - WAIT commands will be skipped")
            reason_overlay("WAITING disabled - all WAIT commands will be skipped")
        else:
            set_skip_waits(False)
            print("[⚡ WAITING] Enabled - WAIT commands will execute normally")
            reason_overlay("WAITING enabled - WAIT commands will wait normally")
        context["last_action"] = step_string
        return True, context

    if command == "TYPE_IN":
        arg_str = argument.strip()
        quote_char = None
        if '"' in arg_str:
            quote_char = '"'
        elif "'" in arg_str:
            quote_char = "'"
            
        if quote_char:
            first_quote = arg_str.find(quote_char)
            element = arg_str[:first_quote].strip()
            text_payload = arg_str[first_quote:].strip(quote_char)
        else:
            parts = arg_str.split(" ", 1)
            element = parts[0]
            text_payload = parts[1] if len(parts) > 1 else ""

        print(f"[⌨️ TYPE_IN] Targeting element: '{element}' with text: '{text_payload}'")
        reason_overlay(f"TYPE_IN '{element}' - typing '{text_payload}'")
        coords = smart_find_element(element)
        if coords:
            print(f"[🎯 MATCH] Click focusing input at: {coords}")
            pyautogui.click(coords[0], coords[1])
            time.sleep(0.5)
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.1)
            pyautogui.press('backspace')
            time.sleep(0.1)
            pyautogui.write(text_payload, interval=0.03)
            time.sleep(0.5)
            context["last_action"] = step_string
            return True, context
        else:
            print(f"[❌ MISSED] Could not locate input target: '{element}'")
            context["last_error"] = f"Could not locate input target: '{element}'"
            return False, context

    if command == "LAUNCH":
        app_name = argument.strip().strip("'").strip('"')
        print(f"[🚀 LAUNCH] Launching: {app_name}")
        reason_overlay(f"LAUNCH '{app_name}' - opening application")
        success = launch_application(app_name)
        if success and app_name.lower() in ["chrome", "google chrome", "msedge", "microsoft edge"]:
            time.sleep(1.0)
            print(f"[🎯 FOCUS] Focusing {app_name} after launch")
            focus_window_by_title(app_name)
        context["last_action"] = step_string
        return success, context

    if command in ["CLICK", "DOUBLE_CLICK", "RIGHT_CLICK"]:
        element_label = argument.strip().strip("'").strip('"')
        click_type = "left"
        if command == "DOUBLE_CLICK":
            click_type = "double"
        elif command == "RIGHT_CLICK":
            click_type = "right"
        print(f"[🎯 SMART CLICK] Using intelligent click for: {element_label} (type: {click_type})")
        reason_overlay(f"CLICK '{element_label}' ({click_type}) - moving mouse and clicking element")
        
        # Special handling for search result clicking
        if element_label.lower() in ["first search result", "search result", "website", "first website"]:
            # Try to extract search query from context to filter out the query text itself
            search_query = None
            last_action = context.get("last_action", "")
            if isinstance(last_action, str) and last_action.lower().startswith("type "):
                query_candidate = last_action.lower().split(" ", 1)[1].strip().strip("'\"")
                if query_candidate and len(query_candidate) > 3:
                    search_query = query_candidate
            success, context = click_first_search_result(context, search_query=search_query)
            return success, context
        
        success, context = smart_click(element_label, context, click_type=click_type)
        return success, context

    if command == "MOVE_TO":
        try:
            parts = argument.strip().split()
            if len(parts) >= 2:
                x, y = int(parts[0]), int(parts[1])
                duration = float(parts[2]) if len(parts) > 2 else 0.3
                pyautogui.moveTo(x, y, duration=duration)
                print(f"[🖱️ MOVE_TO] Moved mouse to ({x}, {y})")
                reason_overlay(f"MOVE_TO ({x}, {y}) - moving cursor")
                context["last_action"] = step_string
                return True, context
        except Exception as e:
            print(f"[❌ MOVE_TO ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "MOVE_REL":
        try:
            parts = argument.strip().split()
            if len(parts) >= 2:
                dx, dy = int(parts[0]), int(parts[1])
                duration = float(parts[2]) if len(parts) > 2 else 0.3
                pyautogui.moveRel(dx, dy, duration=duration)
                print(f"[🖱️ MOVE_REL] Moved mouse by ({dx}, {dy})")
                reason_overlay(f"MOVE_REL ({dx}, {dy}) - relative cursor move")
                context["last_action"] = step_string
                return True, context
        except Exception as e:
            print(f"[❌ MOVE_REL ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "SCROLL":
        try:
            parts = argument.strip().split()
            amount = int(parts[0]) if parts else -3
            pyautogui.scroll(amount)
            direction = "down" if amount > 0 else "up"
            print(f"[🖱️ SCROLL] Scrolled {direction} by {abs(amount)}")
            reason_overlay(f"SCROLL {amount} - scrolled {direction} in active window")
            context["last_action"] = step_string
            return True, context
        except Exception as e:
            print(f"[❌ SCROLL ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "TAB":
        try:
            parts = argument.strip().split()
            count = int(parts[0]) if parts else 1
            for _ in range(count):
                pyautogui.press('tab')
                time.sleep(0.1)
            direction = "right" if count > 0 else "left"
            print(f"[⌨️ TAB] Tabbed {abs(count)} times {direction}")
            reason_overlay(f"TAB {count} - keyboard navigation")
            context["last_action"] = step_string
            return True, context
        except Exception as e:
            print(f"[❌ TAB ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "FIND_TEXT":
        text = argument.strip().strip("'").strip('"')
        print(f"[🔍 FIND_TEXT] Searching for text: '{text}'")
        reason_overlay(f"FIND_TEXT '{text}' - searching for visible text label")
        coords = find_via_ocr(text)
        if coords:
            pyautogui.moveTo(coords[0], coords[1], duration=0.3)
            print(f"[🎯 FIND_TEXT] Found and moved to: {coords}")
            context["last_action"] = step_string
            context["last_found_coords"] = coords
            return True, context
        else:
            print(f"[❌ FIND_TEXT] Text not found: {text}")
            context["last_error"] = f"Text not found: {text}"
            return False, context

    if command == "FIND_IMAGE":
        image_name = argument.strip().strip("'").strip('"')
        print(f"[🔍 FIND_IMAGE] Searching for image: '{image_name}'")
        reason_overlay(f"FIND_IMAGE '{image_name}' - searching for visual template")
        coords = find_via_template_matching(image_name)
        if coords:
            pyautogui.moveTo(coords[0], coords[1], duration=0.3)
            print(f"[🎯 FIND_IMAGE] Found and moved to: {coords}")
            context["last_action"] = step_string
            context["last_found_coords"] = coords
            return True, context
        else:
            print(f"[❌ FIND_IMAGE] Image not found: {image_name}")
            context["last_error"] = f"Image not found: {image_name}"
            return False, context

    # ==========================================
    # FILE OPERATIONS & SELF-CORRECTION COMMANDS
    # ==========================================
    
    if command == "CREATE_FILE":
        arg_str = argument.strip()
        if not arg_str:
            print("[❌ ERROR] CREATE_FILE requires a file path")
            context["last_error"] = "CREATE_FILE requires a file path"
            return False, context
        file_path = arg_str.strip("'").strip('"')
        file_path = os.path.expanduser(file_path)
        full_path = os.path.join(context["workspace"], file_path) if not os.path.isabs(file_path) else file_path
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write("")
            print(f"[📝 CREATE_FILE] Created: {full_path}")
            reason_overlay(f"CREATE_FILE '{file_path}' - file created")
            context["last_action"] = step_string
            return True, context
        except Exception as e:
            print(f"[❌ CREATE_FILE ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "WRITE_FILE":
        arg_str = argument.strip()
        if not arg_str:
            print("[❌ ERROR] WRITE_FILE requires file path and content")
            context["last_error"] = "WRITE_FILE requires file path and content"
            return False, context
        
        # Parse: WRITE_FILE <path> "<content>"
        quote_char = None
        if '"' in arg_str:
            quote_char = '"'
        elif "'" in arg_str:
            quote_char = "'"
            
        if quote_char:
            first_quote = arg_str.find(quote_char)
            file_path = arg_str[:first_quote].strip()
            content = arg_str[first_quote:].strip(quote_char)
        else:
            parts = arg_str.split(" ", 1)
            file_path = parts[0]
            content = parts[1] if len(parts) > 1 else ""
        
        file_path = os.path.expanduser(file_path)
        full_path = os.path.join(context["workspace"], file_path) if not os.path.isabs(file_path) else file_path
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"[📝 WRITE_FILE] Written to: {full_path} ({len(content)} chars)")
            reason_overlay(f"WRITE_FILE '{file_path}' - {len(content)} chars written")
            context["last_action"] = step_string
            return True, context
        except Exception as e:
            print(f"[❌ WRITE_FILE ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "READ_FILE":
        file_path = argument.strip().strip("'").strip('"')
        file_path = os.path.expanduser(file_path)
        full_path = os.path.join(context["workspace"], file_path) if not os.path.isabs(file_path) else file_path
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            print(f"[📖 READ_FILE] Read {len(content)} chars from: {full_path}")
            context["last_file_content"] = content
            context["last_file_path"] = full_path
            context["last_action"] = step_string
            return True, context
        except Exception as e:
            print(f"[❌ READ_FILE ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "DELETE_FILE":
        file_path = argument.strip().strip("'").strip('"')
        file_path = os.path.expanduser(file_path)
        full_path = os.path.join(context["workspace"], file_path) if not os.path.isabs(file_path) else file_path
        try:
            if os.path.exists(full_path):
                os.remove(full_path)
                print(f"[🗑️ DELETE_FILE] Deleted: {full_path}")
            else:
                print(f"[⚠️ DELETE_FILE] File not found: {full_path}")
            context["last_action"] = step_string
            return True, context
        except Exception as e:
            print(f"[❌ DELETE_FILE ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "RUN_FILE":
        file_path = argument.strip().strip("'").strip('"')
        file_path = os.path.expanduser(file_path)
        full_path = os.path.join(context["workspace"], file_path) if not os.path.isabs(file_path) else file_path
        try:
            if not os.path.exists(full_path):
                print(f"[❌ RUN_FILE] File not found: {full_path}")
                context["last_error"] = f"File not found: {full_path}"
                return False, context
            
            ext = os.path.splitext(full_path)[1].lower()
            if ext == '.py':
                cmd = ['python', full_path]
            elif ext in ['.bat', '.cmd']:
                cmd = [full_path]
            elif ext == '.ps1':
                cmd = ['powershell', '-ExecutionPolicy', 'Bypass', '-File', full_path]
            elif ext in ['.exe', '.com']:
                cmd = [full_path]
            else:
                cmd = ['start', '', full_path]
                print(f"[🚀 RUN_FILE] Launching via system association: {full_path}")
                os.system(f'start "" "{full_path}"')
                context["last_action"] = step_string
                return True, context
            
            print(f"[🚀 RUN_FILE] Executing: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(full_path) or context["workspace"], timeout=60)
            print(f"[📤 OUTPUT] stdout: {result.stdout[:500] if result.stdout else '(empty)'}")
            if result.stderr:
                print(f"[⚠️ STDERR] {result.stderr[:500]}")
            context["last_run_result"] = {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
            context["last_action"] = step_string
            return result.returncode == 0, context
        except subprocess.TimeoutExpired:
            print(f"[⏱️ RUN_FILE TIMEOUT] Execution exceeded 60 seconds")
            context["last_error"] = "Execution timeout (60s)"
            return False, context
        except Exception as e:
            print(f"[❌ RUN_FILE ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "LIST_FILES":
        dir_path = argument.strip().strip("'").strip('"') if argument.strip() else "."
        dir_path = os.path.expanduser(dir_path)
        full_path = os.path.join(context["workspace"], dir_path) if not os.path.isabs(dir_path) else dir_path
        try:
            if os.path.isdir(full_path):
                files = os.listdir(full_path)
                print(f"[📁 LIST_FILES] {full_path}: {files}")
                context["last_file_list"] = files
                context["last_action"] = step_string
                return True, context
            else:
                print(f"[❌ LIST_FILES] Not a directory: {full_path}")
                context["last_error"] = f"Not a directory: {full_path}"
                return False, context
        except Exception as e:
            print(f"[❌ LIST_FILES ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "SET_WORKSPACE":
        workspace_path = argument.strip().strip("'").strip('"')
        if os.path.isdir(workspace_path):
            context["workspace"] = workspace_path
            print(f"[📁 WORKSPACE] Changed to: {workspace_path}")
            context["last_action"] = step_string
            return True, context
        else:
            print(f"[❌ WORKSPACE] Directory not found: {workspace_path}")
            context["last_error"] = f"Directory not found: {workspace_path}"
            return False, context

    if command == "SELF_CORRECT":
        print(f"[🔄 SELF_CORRECT] Analyzing last error: {context.get('last_error', 'None')}")
        print(f"[🔄 SELF_CORRECT] Last action: {context.get('last_action', 'None')}")
        reason_overlay(f"SELF_CORRECT - analyzing error: {context.get('last_error', 'None')[:50]}")
        print(f"[🔄 SELF_CORRECT] Workspace: {context.get('workspace', 'N/A')}")
        print(f"[🔄 SELF_CORRECT] Last run result: {context.get('last_run_result', 'N/A')}")
        
        # Build step history for context
        step_history_str = "\n".join([
            f"Step {i+1}: {s['step']} (prompt: {s['prompt'][:60]}) - {'OK' if s['success'] else 'FAILED'}: {s['error'][:60] if s['error'] else 'OK'}"
            for i, s in enumerate(_step_history[-5:])  # Last 5 steps
        ])
        
        # Ask for correction strategy
        try:
            correction_prompt = f"""
            The previous automation step failed. Analyze and provide a corrected step.
            
            Last action: {context.get('last_action', 'None')}
            Last error: {context.get('last_error', 'None')}
            Workspace: {context.get('workspace', 'N/A')}
            Last run result: {context.get('last_run_result', 'N/A')}
            Last file content: {context.get('last_file_content', 'N/A')[:500] if context.get('last_file_content') else 'N/A'}
            Alternative labels tried: {context.get('last_successful_label', 'None')}
            Last screen analysis: {context.get('screen_summary', 'Not yet analyzed')[:300]}
            
            Recent step history:
            {step_history_str}
            
            INTELLIGENT CORRECTION RULES:
            - PREFER KEYBOARD SHORTCUTS over visual clicking
            - For browser address bar: use "HOTKEY ctrl l" (NOT CLICK address_bar)
            - For VS Code: use "VSCODE_ACTION save" (NOT CLICK save)
            - For Antigravity: use "ANTIGRAVITY_ACTION save"
            - If element not found visually, try FOCUS_APP first, then SCREEN_ANALYZE
            - For profile/avatar in Chrome: use SELECT_PROFILE or look for image controls in top-right
            - For videos on YouTube: look for thumbnails with video keywords (funny, horror, etc.)
            - If CLICK failed, try alternative semantic labels or keyboard navigation
            - For chat apps: use CHAT_REPLY, never open new windows
            - For popups/dialogs: try PRESS enter to accept, PRESS escape to cancel
            - File creation: ALWAYS use CREATE_FILE directly, never click file menus
            - For YouTube full screen: look for icon in lower-right corner of video
            - If window title suggests Chrome Profile Picker: inject SELECT_PROFILE
            
            Provide ONE corrected automation step in JSON array format.
            Available commands: LAUNCH, CLICK, DOUBLE_CLICK, RIGHT_CLICK, MOVE_TO, MOVE_REL, SCROLL, FIND_TEXT, FIND_IMAGE, SCREEN_ANALYZE, CHAT_REPLY, TYPE_IN, TYPE, PRESS, HOTKEY, WAIT, CREATE_FILE, WRITE_FILE, READ_FILE, DELETE_FILE, RUN_FILE, LIST_FILES, SET_WORKSPACE, SELECT_PROFILE, VSCODE_ACTION, ANTIGRAVITY_ACTION, FOCUS_APP, EXPECT, SELF_CORRECT, BROWSER_ACTION
            
            Output ONLY a JSON array with one corrected step.
            """
            response = ollama_chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": correction_prompt}],
                temperature=0.1
            )
            raw = response['choices'][0]['message']['content']
            import re
            match = re.search(r'\[.*?\]', raw, re.DOTALL)
            if match:
                corrected_steps = json.loads(match.group())
                if corrected_steps:
                    step = corrected_steps[0]
                    if isinstance(step, dict):
                        cmd = step.get("command", "").upper()
                        arg = step.get("target", step.get("element", ""))
                        if cmd == "CLICK":
                            step = f"CLICK {arg}"
                        elif cmd == "FIND_TEXT":
                            step = f"FIND_TEXT {arg}"
                        elif cmd == "FIND_IMAGE":
                            step = f"FIND_IMAGE {arg}"
                        elif cmd in ("HOTKEY", "PRESS", "TYPE", "TYPE_IN", "WAIT"):
                            step = f"{cmd} {arg}"
                        elif cmd in ("LAUNCH", "FOCUS_APP", "SCREEN_ANALYZE"):
                            step = f"{cmd} {arg}"
                        elif cmd in ("BROWSER_ACTION", "VSCODE_ACTION", "ANTIGRAVITY_ACTION"):
                            step = f"{cmd} {arg}"
                        elif cmd in ("CREATE_FILE", "WRITE_FILE", "READ_FILE", "DELETE_FILE", "RUN_FILE", "LIST_FILES", "SET_WORKSPACE"):
                            step = f"{cmd} {arg}"
                        else:
                            step = str(step)
                    print(f"[🔄 SELF_CORRECT] Suggested correction: {step}")
                    return execute_macro_step(step, context)
        except Exception as e:
            print(f"[❌ SELF_CORRECT ERROR] {e}")
        
        context["retry_count"] = context.get("retry_count", 0) + 1
        context["last_action"] = step_string
        return False, context

    if command == "BROWSER_ACTION":
        """Special browser actions via keyboard shortcuts and URL detection"""
        action = argument.strip().strip("'").strip('"').lower()
        browser_shortcuts = {
            "focus_address": ("ctrl", "l"),
            "new_tab": ("ctrl", "t"),
            "close_tab": ("ctrl", "w"),
            "refresh": ("ctrl", "r"),
            "back": ("alt", "left"),
            "forward": ("alt", "right"),
            "go_home": ("alt", "home"),
            "bookmarks": ("ctrl", "b"),
            "history": ("ctrl", "h"),
            "downloads": ("ctrl", "j"),
            "dev_tools": ("f12"),
            "dev_tools_elements": ("ctrl", "shift", "c"),
            "dev_tools_console": ("ctrl", "shift", "j"),
        }
        
        if action in browser_shortcuts:
            keys = browser_shortcuts[action]
            window_name = context.get("screen_active_window", "").lower()
            is_browser = any(b in window_name for b in ["chrome", "edge", "firefox", "brave"])
            
            if not is_browser:
                reason_overlay(f"BROWSER_ACTION '{action}' BLOCKED - not in browser (active: {context.get('screen_active_window', 'unknown')}). Must LAUNCH chrome or FOCUS_APP chrome first.")
                context["last_error"] = f"Cannot use BROWSER_ACTION '{action}' - not in browser window. Current: {context.get('screen_active_window', 'unknown')}"
                return False, context
            
            print(f"[🌐 BROWSER_ACTION] {action} -> {keys}")
            reason_overlay(f"BROWSER_ACTION '{action}' - using {' + '.join(keys)} in browser")
            pyautogui.hotkey(*keys)
            time.sleep(0.3 if get_skip_waits() else 0.5)
            context["last_action"] = step_string
            return True, context
        else:
            print(f"[❌ BROWSER_ACTION] Unknown action: {action}. Available: {list(browser_shortcuts.keys())}")
            context["last_error"] = f"Unknown browser action: {action}"
            return False, context

    if command == "SCREEN_ANALYZE":
        """Analyze the current screen: active window, visible text, and regions."""
        detect_screen_context(context)
        reason_overlay(f"SCREEN_ANALYZE - checking active window and visible elements")
        context["last_action"] = step_string
        return True, context

    if command == "CHAT_REPLY":
        """For chat apps (Discord, Slack, etc.): analyze visible messages, generate a contextual reply, type and send it."""
        try:
            detect_screen_context(context)
            reason_overlay(f"CHAT_REPLY - analyzing chat in {context.get('screen_active_window', 'unknown')}")
            text_blocks = context.get("screen_text_blocks", [])
            region_labels = context.get("screen_region_labels", [])
            window_name = context.get("screen_active_window", "")
            screen_explanation = _get_screen_context_explanation(
                window_name,
                context.get("screen_is_ide", False),
                context.get("screen_is_browser", False),
                context.get("screen_ide_type", "")
            )
            history = _get_conversation_summary()
            
            # Build context of visible messages
            visible_text = "\n".join(text_blocks[:20]) if text_blocks else "No visible text"
            if region_labels:
                visible_text += "\n[Regions]:\n" + "\n".join(region_labels[:10])
            
            # Ask for a contextual reply
            reply_prompt = f"""You are chatting in {window_name}. Screen context: {screen_explanation}

Visible messages on screen:
{visible_text}

Previous conversation:
{history}

Generate a short, natural chat reply (1-2 sentences) that fits the conversation. Be casual and engaging.
Reply with ONLY the message text, nothing else."""
            
            response = ollama_chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": reply_prompt}],
                temperature=0.7,
                max_tokens=300
            )
            reply_text = response['choices'][0]['message']['content'].strip()
            # Clean up common LLM artifacts
            reply_text = reply_text.strip('"').strip("'")
            if reply_text.startswith("Reply:"):
                reply_text = reply_text[6:].strip()
            
            print(f"[💬 CHAT_REPLY] Generated reply: {reply_text[:100]}")
            _add_conversation("AI", reply_text, meta={
                "window": window_name,
                "screen_summary": context.get("screen_summary", ""),
                "type": "chat_reply"
            })
            
            # Find the message input box (usually at the bottom)
            input_coords = None
            # Try to find common input box indicators
            for label in ["message", "type a message", "chat input", "text input", "send a message"]:
                coords = find_via_ocr(label)
                if coords:
                    input_coords = coords
                    break
            
            if not input_coords:
                # Fallback: click near bottom center of screen where chat inputs usually are
                screen_w, screen_h = pyautogui.size()
                input_coords = (screen_w // 2, int(screen_h * 0.92))
            
            pyautogui.click(input_coords[0], input_coords[1])
            time.sleep(0.3)
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.1)
            pyautogui.press('backspace')
            time.sleep(0.1)
            pyautogui.write(reply_text, interval=0.02)
            time.sleep(0.3)
            pyautogui.press('enter')
            time.sleep(1.0)
            
            context["last_reply"] = reply_text
            context["last_action"] = step_string
            return True, context
        except Exception as e:
            print(f"[❌ CHAT_REPLY ERROR] {e}")
            context["last_error"] = str(e)
            return False, context

    if command == "FOCUS_APP":
        """Focus a specific application window by name."""
        app_name = argument.strip().strip("'").strip('"')
        reason_overlay(f"FOCUS_APP '{app_name}' - focusing window")
        if focus_window_by_title(app_name):
            context["last_action"] = step_string
            return True, context
        else:
            context["last_error"] = f"Could not focus window: {app_name}"
            return False, context

    if command == "SELECT_PROFILE":
        """Automatically select the first user profile in Chrome's profile picker."""
        print("[👤 SELECT_PROFILE] Attempting to select Chrome profile...")
        reason_overlay("SELECT_PROFILE - choosing first Chrome profile")
        success = _select_chrome_profile(context)
        if success:
            context["last_action"] = step_string
            return True, context
        else:
            context["last_error"] = "Failed to select Chrome profile"
            return False, context

    if command == "VSCODE_ACTION":
        """Special VS Code actions via keyboard shortcuts"""
        action = argument.strip().strip("'").strip('"').lower()
        vscode_shortcuts = {
            "new_file": ("ctrl", "n"),
            "open_file": ("ctrl", "o"),
            "save": ("ctrl", "s"),
            "save_all": ("ctrl", "k", "s"),
            "close_tab": ("ctrl", "w"),
            "close_window": ("ctrl", "shift", "w"),
            "split_editor": ("ctrl", "\\"),
            "toggle_sidebar": ("ctrl", "b"),
            "toggle_panel": ("ctrl", "j"),
            "toggle_terminal": ("ctrl", "`"),
            "command_palette": ("ctrl", "shift", "p"),
            "quick_open": ("ctrl", "p"),
            "search_files": ("ctrl", "shift", "f"),
            "search_in_file": ("ctrl", "f"),
            "replace_in_file": ("ctrl", "h"),
            "go_to_line": ("ctrl", "g"),
            "format_document": ("shift", "alt", "f"),
            "comment_line": ("ctrl", "/"),
            "duplicate_line": ("shift", "alt", "down"),
            "move_line_up": ("alt", "up"),
            "move_line_down": ("alt", "down"),
            "select_next": ("ctrl", "d"),
            "select_all_occurrences": ("ctrl", "shift", "l"),
            "zen_mode": ("ctrl", "k", "z"),
            "run_code": ("ctrl", "f5"),
            "debug_start": ("f5"),
            "debug_stop": ("shift", "f5"),
            "step_over": ("f10"),
            "step_into": ("f11"),
            "open_settings": ("ctrl", ","),
            "open_keyboard_shortcuts": ("ctrl", "k", "ctrl", "s"),
        }
        
        if action in vscode_shortcuts:
            keys = vscode_shortcuts[action]
            print(f"[⌨️ VSCODE_ACTION] {action} -> {keys}")
            pyautogui.hotkey(*keys)
            time.sleep(0.5)
            context["last_action"] = step_string
            return True, context
        else:
            print(f"[❌ VSCODE_ACTION] Unknown action: {action}. Available: {list(vscode_shortcuts.keys())}")
            context["last_error"] = f"Unknown VS Code action: {action}"
            return False, context

    if command == "ANTIGRAVITY_ACTION":
        """Special Antigravity IDE actions via keyboard shortcuts and UI navigation"""
        action = argument.strip().strip("'").strip('"').lower()
        antigrav_shortcuts = {
            "new_file": ("ctrl", "n"),
            "open_file": ("ctrl", "o"),
            "save": ("ctrl", "s"),
            "save_all": ("ctrl", "shift", "s"),
            "close_tab": ("ctrl", "w"),
            "close_window": ("ctrl", "shift", "w"),
            "run_code": ("f5"),
            "debug_start": ("f5"),
            "debug_stop": ("shift", "f5"),
            "search_files": ("ctrl", "shift", "f"),
            "search_in_file": ("ctrl", "f"),
            "replace_in_file": ("ctrl", "h"),
            "command_palette": ("ctrl", "shift", "p"),
            "format_document": ("ctrl", "alt", "f"),
            "toggle_sidebar": ("ctrl", "b"),
            "toggle_terminal": ("ctrl", "`"),
            "comment_line": ("ctrl", "/"),
            "duplicate_line": ("ctrl", "d"),
            "select_all": ("ctrl", "a"),
            "cut": ("ctrl", "x"),
            "copy": ("ctrl", "c"),
            "paste": ("ctrl", "v"),
            "undo": ("ctrl", "z"),
            "redo": ("ctrl", "y"),
        }
        
        if action in antigrav_shortcuts:
            keys = antigrav_shortcuts[action]
            print(f"[🚀 ANTIGRAVITY_ACTION] {action} -> {keys}")
            pyautogui.hotkey(*keys)
            time.sleep(0.5)
            context["last_action"] = step_string
            return True, context
        else:
            print(f"[❌ ANTIGRAVITY_ACTION] Unknown action: {action}. Available: {list(antigrav_shortcuts.keys())}")
            context["last_error"] = f"Unknown Antigravity action: {action}"
            return False, context

    if command == "CREATE_PROJECT":
        """Create a project folder structure in VS Code or Antigravity IDE via UI interaction"""
        args = argument.strip()
        if not args:
            print("[❌ CREATE_PROJECT] Requires project path and name")
            context["last_error"] = "CREATE_PROJECT requires arguments"
            return False, context
        
        parts = args.split(None, 1)
        if len(parts) >= 2:
            project_path = parts[0].strip("'").strip('"')
            project_name = parts[1].strip("'").strip('"')
        else:
            project_name = parts[0].strip("'").strip('"')
            project_path = os.path.join(context["workspace"], project_name)
        
        window_result = _detect_ide_window()
        if window_result == "vscode":
            print(f"[📁 CREATE_PROJECT] VS Code detected, creating project via UI...")
            return _create_project_vscode(project_path, project_name, context)
        elif window_result == "antigravity":
            print(f"[📁 CREATE_PROJECT] Antigravity IDE detected, creating project via UI...")
            return _create_project_antigravity(project_path, project_name, context)
        else:
            print(f"[❌ CREATE_PROJECT] No IDE window detected. Use LAUNCH vscode or LAUNCH antigravity first.")
            context["last_error"] = "No IDE window focused"
            return False, context

    if command == "WRITE_IN_CODE":
        """Write code directly into the currently focused code editor via UI"""
        content = argument.strip().strip("'").strip('"')
        if not content:
            print("[❌ WRITE_IN_CODE] Requires content to write")
            context["last_error"] = "WRITE_IN_CODE requires content"
            return False, context
        
        editor_coords = _find_code_editor_area()
        if editor_coords:
            print(f"[⌨️ WRITE_IN_CODE] Clicking into editor area at {editor_coords}")
            pyautogui.click(editor_coords[0], editor_coords[1])
            time.sleep(0.5)
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.2)
            pyautogui.press('backspace')
            time.sleep(0.2)
            pyautogui.write(content, interval=0.01)
            context["last_action"] = step_string
            return True, context
        else:
            print("[❌ WRITE_IN_CODE] Could not find editor area")
            context["last_error"] = "Could not locate code editor area"
            return False, context

    if command == "OPEN_FILE_EXPLORER":
        """Open file explorer sidebar in VS Code/Antigravity IDE via UI click"""
        window_result = _detect_ide_window()
        if window_result in ["vscode", "antigravity"]:
            coords = smart_find_element("explorer")
            if coords:
                pyautogui.click(coords[0], coords[1])
                time.sleep(0.5)
                context["last_action"] = step_string
                return True, context
            else:
                if window_result == "vscode":
                    return execute_macro_step("VSCODE_ACTION toggle_sidebar", context)
                else:
                    return execute_macro_step("ANTIGRAVITY_ACTION toggle_sidebar", context)
        else:
            print("[❌ OPEN_FILE_EXPLORER] No IDE window detected")
            context["last_error"] = "No IDE window focused"
            return False, context
    
    if command == "EXPECT":
        parts = argument.strip().split(None, 2)
        if len(parts) < 3:
            print("[❌ EXPECT] Requires: <element> <expectation_type> <value>")
            context["last_error"] = "EXPECT requires 3 arguments"
            return False, context
        element_label = parts[0].strip("'").strip('"')
        expect_type = parts[1].strip("'").strip('"')
        value = parts[2].strip("'").strip('"')
        reason_overlay(f"EXPECT '{element_label}' - setting {expect_type} expectation")
        if expect_type == "window_title":
            set_expectation(element_label, {"window_title": value})
            print(f"[📝 EXPECT] Set window_title expectation '{value}' for '{element_label}'")
        elif expect_type == "text_contains":
            vals = [v.strip() for v in value.split(",")]
            set_expectation(element_label, {"text_contains": vals})
            print(f"[📝 EXPECT] Set text_contains expectation for '{element_label}'")
        elif expect_type == "text_contains_any":
            vals = [v.strip() for v in value.split(",")]
            set_expectation(element_label, {"text_contains_any": vals})
            print(f"[📝 EXPECT] Set text_contains_any expectation for '{element_label}'")
        else:
            print(f"[❌ EXPECT] Unknown expectation type: {expect_type}")
            context["last_error"] = f"Unknown expectation type: {expect_type}"
            return False, context
        context["last_action"] = step_string
        return True, context
    
    context["last_error"] = f"Unknown command: {command}"
    return False, context

# ==========================================
# TASK DECOMPOSITION
# ==========================================

def _generate_conversational_reply(prompt, context=None):
    """Generate a helpful conversational reply for research/info-style prompts."""
    history = _get_conversation_summary()
    ctx = context or {}
    window = ctx.get("screen_active_window", "")
    is_ide = ctx.get("screen_is_ide", False)
    is_browser = ctx.get("screen_is_browser", False)
    ide_type = ctx.get("screen_ide_type", "")
    screen_explanation = _get_screen_context_explanation(window, is_ide, is_browser, ide_type)
    
    reply_prompt = (
        f"The user asked: \"{prompt}\"\n\n"
        f"Screen context: {screen_explanation}\n\n"
        f"Previous conversation:\n{history}\n\n"
        "Reply naturally and conversationally in 1-2 sentences. "
        "If you want to take an action, say it explicitly using phrases like: "
        "'I'll open Chrome for you', 'let me search for X', 'I'll navigate to X', 'I'll click on X'. "
        "The system will detect and execute those actions automatically. "
        "Otherwise, offer to help without executing tools."
    )
    response = ollama_chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "user", "content": reply_prompt}],
        temperature=0.7,
        max_tokens=256
    )
    text = response.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    return text or "Sure, here are some helpful options. Want me to open a website, search for it, or help with another task?"

def _is_analysis_prompt(prompt_lower):
    analysis_keywords = [
        "explain what my screen is showing",
        "explain my code",
        "explain the code",
        "what do you see",
        "what is on my screen",
        "describe my screen",
        "analyze my screen",
        "summarize my screen",
        "describe the code",
        "explain this code",
        "explain this",
        "what is this",
        "whats on my screen",
        "what's on my screen",
        "show me what you see",
        "tell me what you see"
    ]
    return any(kw in prompt_lower for kw in analysis_keywords)

def _is_chat_message(prompt_lower):
    """Heuristically detect conversational/correction messages that should NOT be executed as automation tasks."""
    # Strip punctuation for matching
    clean = prompt_lower.strip().strip(".!?,")
    
    # Explicit corrections / negations
    correction_markers = [
        "thats not", "that's not", "that is not", "not a", "not the",
        "no ", "wrong", "incorrect", "not right", "not correct",
        "that didnt", "that didn't", "didn't work", "didnt work",
        "failed", "failure", "error", "not good", "bad", "stop",
        "cancel", "abort", "never mind", "nevermind", "forget it",
        "i meant", "i mean ", "instead", "not that", "not what i",
    ]
    if any(m in clean for m in correction_markers):
        return True
    
    # Short acknowledgments / conversational fillers
    chat_words = [
        "ok", "okay", "sure", "yes", "no", "nope", "yep", "yeah",
        "thanks", "thank you", "thx", "np", "no problem", "alright",
        "got it", "understood", "fine", "good", "great", "nice",
        "hello", "hi ", "hey", "sup", "yo ", "whats up", "what's up",
        "lol", "haha", "nice one", "cool", "perfect", "excellent",
    ]
    words = clean.split()
    if len(words) <= 4 and any(w in chat_words for w in words):
        return True
    
    # Pure questions without automation verbs
    question_starters = ["why", "how", "what ", "when", "where", "who ", "which", "can you", "could you", "would you", "are you", "is it", "does it"]
    if any(clean.startswith(q) for q in question_starters):
        # Unless it's clearly an automation request like "how do I open chrome"
        automation_question_markers = ["open", "launch", "click", "type", "search", "find", "go to", "run", "execute", "do this", "do that"]
        if not any(m in clean for m in automation_question_markers):
            return True
    
    # Very short messages without action verbs are likely chat
    action_verbs = ["open", "launch", "close", "click", "type", "search", "find", "go", "make", "create", "build", "run", "execute", "start", "stop", "wait", "focus", "save", "delete", "write", "read", "scroll", "press", "hotkey", "navigate", "select", "switch", "install", "download", "watch", "play", "launch", "focus"]
    if len(words) <= 3 and not any(v in clean for v in action_verbs):
        return True
    
    # Conversational phrases that contain action words but aren't tasks
    conversational_phrases = [
        "try searching", "try finding", "try looking", "could you search", "could you find",
        "can you search", "can you find", "do you know", "what is", "what are", "what was",
        "how do i", "how does", "tell me about", "explain", "describe", "what about",
        "remember when", "we talked about", "like we talked", "like before",
        "find it", "search it", "look for it", "find that", "search that", "look for that",
    ]
    if any(clean.startswith(p) or p in clean for p in conversational_phrases):
        return True
    
    # Vague pronoun-only search intents without a clear object
    vague_pronoun_patterns = ["find it", "search it", "look for it", "find that", "search that", "look for that"]
    if any(clean == p for p in vague_pronoun_patterns):
        return True
    
    return False

def _extract_actions_from_reply(reply_text):
    """Extract executable actions from an LLM chat reply.
    
    Returns a list of executable step strings, or empty list if no actions detected.
    This allows the LLM to say 'I'll open Chrome for you' and actually open Chrome.
    """
    if not reply_text or not isinstance(reply_text, str):
        return []
    
    reply_lower = reply_text.lower().strip()
    actions = []
    
    # Opening apps - match specific app names only
    app_aliases = {
        "chrome": "chrome",
        "browser": "chrome",
        "edge": "edge",
        "firefox": "firefox",
        "explorer": "explorer",
        "file explorer": "explorer",
        "vscode": "vscode",
        "visual studio code": "vscode",
        "antigravity": "antigravity",
        "notepad": "notepad",
        "calculator": "calculator",
    }
    
    for alias, app_name in app_aliases.items():
        if f"i'll open {alias}" in reply_lower or f"let me open {alias}" in reply_lower or f"opening {alias}" in reply_lower:
            actions.append(f"LAUNCH {app_name}")
    
    # Special case: opening python learning sites
    if "i'll open" in reply_lower and "python" in reply_lower and ("learning" in reply_lower or "tutorial" in reply_lower or "course" in reply_lower):
        actions = ["HOTKEY ctrl l", "TYPE python.org", "PRESS enter", "WAIT 2.0"]
        return actions
    
    # Special case: searching for something
    if "i'll search for" in reply_lower or "let me search for" in reply_lower or "searching for" in reply_lower:
        for prefix in ["i'll search for ", "let me search for ", "searching for "]:
            if prefix in reply_lower:
                query = reply_lower.split(prefix, 1)[1].split(".")[0].split(",")[0].strip()
                if query:
                    actions.append(f"TYPE {query}")
                    actions.append("PRESS enter")
                    break
    
    # Special case: navigating to a website
    if "i'll navigate to" in reply_lower or "let me navigate to" in reply_lower or "navigating to" in reply_lower:
        for prefix in ["i'll navigate to ", "let me navigate to ", "navigating to "]:
            if prefix in reply_lower:
                site = reply_lower.split(prefix, 1)[1].split(".")[0].split(",")[0].strip()
                if site:
                    actions.append(f"HOTKEY ctrl l")
                    actions.append(f"TYPE {site}")
                    actions.append("PRESS enter")
                    break
    
    # Special case: typing something
    if "i'll type" in reply_lower or "let me type" in reply_lower:
        for prefix in ["i'll type ", "let me type "]:
            if prefix in reply_lower:
                text = reply_lower.split(prefix, 1)[1].split(".")[0].split(",")[0].strip()
                if text:
                    actions.append(f"TYPE {text}")
                    break
    
    # Special case: clicking something
    if "i'll click on" in reply_lower or "let me click on" in reply_lower or "clicking on" in reply_lower:
        for prefix in ["i'll click on ", "let me click on ", "clicking on "]:
            if prefix in reply_lower:
                target = reply_lower.split(prefix, 1)[1].split(".")[0].split(",")[0].strip()
                if target:
                    actions.append(f"CLICK {target}")
                    break
    
    return actions
    
    # Special case: searching for something
    if "i'll search for" in reply_lower or "let me search for" in reply_lower or "searching for" in reply_lower:
        for prefix in ["i'll search for ", "let me search for ", "searching for "]:
            if prefix in reply_lower:
                query = reply_lower.split(prefix, 1)[1].split(".")[0].split(",")[0].strip()
                if query:
                    actions.append(f"TYPE {query}")
                    actions.append("PRESS enter")
                    break
    
    # Special case: navigating to a website
    if "i'll navigate to" in reply_lower or "let me navigate to" in reply_lower or "navigating to" in reply_lower:
        for prefix in ["i'll navigate to ", "let me navigate to ", "navigating to "]:
            if prefix in reply_lower:
                site = reply_lower.split(prefix, 1)[1].split(".")[0].split(",")[0].strip()
                if site:
                    actions.append(f"HOTKEY ctrl l")
                    actions.append(f"TYPE {site}")
                    actions.append("PRESS enter")
                    break
    
    # Special case: typing something
    if "i'll type" in reply_lower or "let me type" in reply_lower:
        for prefix in ["i'll type ", "let me type "]:
            if prefix in reply_lower:
                text = reply_lower.split(prefix, 1)[1].split(".")[0].split(",")[0].strip()
                if text:
                    actions.append(f"TYPE {text}")
                    break
    
    # Special case: clicking something
    if "i'll click on" in reply_lower or "let me click on" in reply_lower or "clicking on" in reply_lower:
        for prefix in ["i'll click on ", "let me click on ", "clicking on "]:
            if prefix in reply_lower:
                target = reply_lower.split(prefix, 1)[1].split(".")[0].split(",")[0].strip()
                if target:
                    actions.append(f"CLICK {target}")
                    break
    
    return actions

def _generate_visual_chat_reply(prompt, context):
    """Generate a conversational reply analyzing what's currently on screen."""
    window = context.get('screen_active_window', 'unknown')
    is_ide = context.get('screen_is_ide', False)
    is_browser = context.get('screen_is_browser', False)
    ide_type = context.get('screen_ide_type', '')

    text_blocks = context.get("screen_text_blocks", [])
    region_labels = context.get("screen_region_labels", [])

    visible_text = "\n".join([b["desc"] for b in text_blocks[:20]]) if text_blocks else "No readable text detected"
    regions_text = "\n".join([r["desc"] for r in region_labels[:10]]) if region_labels else "No distinct regions detected"
    screen_explanation = _get_screen_context_explanation(window, is_ide, is_browser, ide_type)

    history = _get_conversation_summary()

    reply_prompt = f"""You are a helpful visual AI assistant. The user is asking about what's currently on their screen.

Screen context: {screen_explanation}

Visible text on screen:
{visible_text}

Visual regions detected:
{regions_text}

Previous conversation:
{history}

User asked: "{prompt}"

Reply naturally and conversationally. Start by briefly stating what window/app is active and what it shows. Explain any code or content if readable, and ask follow-up questions if needed. Keep it under 3 sentences.

If you want to take an action, say it explicitly using phrases like: 'I'll open Chrome for you', 'let me search for X', 'I'll navigate to X', 'I'll click on X'. The system will detect and execute those actions automatically."""

    response = ollama_chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "user", "content": reply_prompt}],
        temperature=0.7,
        max_tokens=300
    )
    text = response.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    return text or "I can see your screen, but the content isn't clear enough for me to analyze in detail. Could you tell me more about what you're looking at?"

def decompose_task(prompt, context=None):
    """Break a complex user prompt into smaller sequential sub-tasks for reliable execution."""
    try:
        window = "unknown"
        is_youtube = False
        is_browser = False
        is_ide = False
        ide_type = ""
        if context:
            window = context.get('screen_active_window', 'unknown')
            is_youtube = context.get('screen_is_youtube', False)
            is_browser = context.get('screen_is_browser', False)
            is_ide = context.get('screen_is_ide', False)
            ide_type = context.get('screen_ide_type', '')
        
        context_hint = ""
        if is_youtube:
            context_hint = "\nNote: Currently on YouTube"
        elif is_browser:
            context_hint = "\nNote: Currently in a browser (Chrome/Edge)"
        elif is_ide:
            context_hint = f"\nNote: Currently in IDE ({ide_type})"
        
        prompt_lower = prompt.lower()
        
        if _is_analysis_prompt(prompt_lower):
            chat_reply = _generate_visual_chat_reply(prompt, context or {})
            _add_conversation("AI", chat_reply, meta={
                "window": (context or {}).get("screen_active_window", ""),
                "screen_summary": (context or {}).get("screen_summary", ""),
                "type": "chat_reply"
            })
            raise ChatReply(chat_reply)
        
        if _is_chat_message(prompt_lower):
            chat_reply = _generate_conversational_reply(prompt, context or {})
            _add_conversation("AI", chat_reply, meta={
                "window": (context or {}).get("screen_active_window", ""),
                "screen_summary": (context or {}).get("screen_summary", ""),
                "type": "chat_reply"
            })
            raise ChatReply(chat_reply)
        
        install_fallback = _fallback_decompose(prompt_lower, is_ide, ide_type)
        if install_fallback:
            print(f"[📋 DECOMPOSE] Broken into {len(install_fallback)} steps: {install_fallback}")
            return _clean_plan(install_fallback, prompt)
        
        # Safety net: browser search/click tasks must never fall through to chat
        if any(k in prompt_lower for k in ["open google", "go to google", "search", "find"]) and any(k in prompt_lower for k in ["tap", "click", "open ", "visit "]):
            query = prompt_lower
            for prefix in ["search for", "search", "find", "find me", "open google", "go to google", "navigate to "]:
                query = query.replace(prefix, "", 1).strip()
            query = re.sub(r"\s+", " ", query).strip()
            if not query:
                query = "minecraft"
            print(f"[📋 DECOMPOSE] Safety browser-search plan for: '{query}'")
            return _clean_plan([
                "launch chrome",
                "focus address bar",
                "type google.com",
                "press enter",
                "wait 2.0",
                f"type {query}",
                "press enter",
                "wait 2.0",
                "click first search result",
            ], prompt)
        
        is_research_task = any(kw in prompt_lower for kw in ["find ways to make money", "search for ways", "how to make money", "trusted platform", "ways to make money"])
        has_build_component = any(kw in prompt_lower for kw in ["open a platform", "build a platform", "make a platform", "create a platform", "open trusted platform", "make website", "create website", "build website"])
        
        if is_research_task and not has_build_component:
            chat_reply = _generate_conversational_reply(prompt, context or {})
            raise ChatReply(chat_reply)
        
        task_type_hint = ""
        if is_research_task and has_build_component:
            task_type_hint = "\nTASK TYPE: This is a COMBINED RESEARCH + BUILD task. First do research (1 step), THEN do build (1 step). Do NOT interleave research and build steps."
        elif is_research_task:
            task_type_hint = "\nTASK TYPE: Research task. Use browser actions."
        elif has_build_component:
            task_type_hint = "\nTASK TYPE: Build/creation task. Use IDE actions."
        
        decompose_prompt = f"""Break this task into 4-8 granular steps. Each step is ONE clear action phrase.{context_hint}{task_type_hint}

Task: {prompt}

Examples:
- "open youtube" -> ["launch chrome", "go to youtube.com", "search for horror videos", "click first video", "press f"]
- "find horror videos" -> ["launch chrome", "search for horror videos on youtube", "click first video"]
- "make a project in antigravity thats a website" -> ["launch antigravity", "create website project in antigravity"]
- "open a new antigravity window, make a project for a good website, open the folder and make website" -> ["launch antigravity", "create website project in antigravity"]
- "find ways to make money and open a trusted platform that pays through paypal" -> ["search for trusted platforms that pay through paypal", "create easy money platform website in antigravity"]
- "open google and search minecraft and tap on official minecraft site" -> ["launch chrome", "focus address bar", "type google.com", "press enter", "wait 2.0", "type minecraft", "press enter", "wait 2.0", "click first search result"]
- "search minecraft and go to official site" -> ["launch chrome", "focus address bar", "type google.com", "press enter", "wait 2.0", "type minecraft", "press enter", "wait 2.0", "click first search result"]

CRITICAL RULES:
- NEVER respond with CHAT_RESPONSE or ask for confirmation. ALWAYS output a JSON plan.
- Step 1: ALWAYS start with "launch chrome" if browser is needed for research
- For browser search tasks: NEVER include the word "search" inside a TYPE argument. Use "type minecraft", NOT "type search minecraft".
- For tasks with "search X and go to Y" or "search X and click Y": break into: launch chrome -> navigate to google -> type X -> press enter -> wait -> click first result
- For "go to Y" or "visit Y" after searching: ALWAYS add "click first search result" as the final step
- For Antigravity/VS Code projects: use "create website project in antigravity" or "create new project in vscode" 
- For general browser search: use "search for X on youtube" or "search for X online"
- Each step: 3-8 words, simple action
- NEVER duplicate project creation steps
- NEVER generate "open folder", "open and make website", or "start building" as separate steps
- If Antigravity is already open and user asks for "new antigravity window", still include "launch antigravity" as first step
- Output JSON array:
["step1", "step2", "step3"]"""

        history = _get_conversation_summary()
        response = ollama_chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": "You are an intent router and conversational assistant. Decide whether the user's message is an automation task or not. If it is NOT an automation task, respond with EXACTLY: CHAT_RESPONSE: <your actual conversational reply>. If it IS an automation task, respond with ONLY a valid JSON array of short action phrases. No explanations."},
                {"role": "user", "content": f"User message: {prompt}\n\nContext:\n{context_hint}{task_type_hint}\n\nConversation history:\n{history}\n\nReply naturally as a helpful assistant. If this is chat, reply like: CHAT_RESPONSE: Hello! How can I help? Otherwise output a JSON array like [\"launch chrome\", \"go to youtube.com\"]"}
            ],
            temperature=0.0,
            max_tokens=256
        )
        
        raw = response['choices'][0]['message']['content']
        print(f"[🔍 DECOMPOSE DEBUG] Raw LLM response: {raw[:200]}")
        
        chat_reply_text = None
        if raw.strip().upper().startswith("CHAT_RESPONSE"):
            chat_reply_text = raw.strip()[len("CHAT_RESPONSE"):].strip(": ").strip() or raw.strip()
        
        # Check for explicit chat reply
        if chat_reply_text:
            raise ChatReply(chat_reply_text)
        
        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if match:
            json_str = match.group()
            print(f"[🔍 DECOMPOSE DEBUG] Extracted JSON: {json_str[:200]}")
            steps, parse_error = _safe_json_loads(json_str)
            if steps is None:
                print(f"[⚠️ DECOMPOSE] JSON parse error: {parse_error}")
                raise ChatReply(raw)
            if isinstance(steps, list) and len(steps) > 0:
                valid_steps = []
                for step in steps:
                    if not isinstance(step, str):
                        print(f"[⚠️ DECOMPOSE] Skipping non-string step: {step!r}")
                        continue
                    step_lower = step.lower()
                    if any(garbage in step_lower for garbage in ["line ", "execute", "def ", "prompt =", "print prompt", "open python", "open maxai2", "find line"]):
                        print(f"[⚠️ DECOMPOSE] Skipping garbage step: {step}")
                        continue
                    if any(skip in step_lower for skip in ["open and make website", "open folder", "start building", "open the folder"]):
                        print(f"[⚠️ DECOMPOSE] Skipping redundant step: {step}")
                        continue
                    valid_steps.append(step)
                
                if len(valid_steps) >= 1:
                    # Echo guard: if the model just wrapped the user prompt in JSON, treat as chat
                    normalized_steps = [str(s).strip().lower() for s in valid_steps]
                    if len(normalized_steps) == 1 and normalized_steps[0] == prompt.strip().lower():
                        raise ChatReply(raw)
                    valid_steps = [s for s in valid_steps if _is_task_step(s)]
                    if not valid_steps:
                        raise ChatReply(raw)
                    print(f"[📋 DECOMPOSE] Broken into {len(valid_steps)} steps: {valid_steps}")
                    return _clean_plan(valid_steps, prompt)
        
        # If the model gave a plain-text response instead of JSON, treat it as chat
        cleaned = raw.strip()
        if cleaned:
            raise ChatReply(cleaned)

        return _clean_plan([prompt], prompt)
    except ChatReply:
        raise
    except Exception as e:
        print(f"[⚠️ DECOMPOSE] Error: {e}")
        return _clean_plan([prompt], prompt)


def _fallback_decompose(prompt_lower, is_ide, ide_type):
    """Fallback task decomposition based on keywords."""
    fallback_steps = []
    
    # Special case: open a learning python site (must be before generic checks)
    if "open" in prompt_lower and "python" in prompt_lower and ("learning" in prompt_lower or "tutorial" in prompt_lower or "course" in prompt_lower or "site" in prompt_lower):
        return ["launch chrome", "focus address bar", "type python.org", "press enter", "wait 2.0"]
    
    if "youtube" in prompt_lower or "video" in prompt_lower:
        # Extract search terms: remove "on youtube" and "youtube" but keep channel/keyword names
        query = prompt_lower.replace("on youtube", "").replace("youtube", "")
        
        # Remove conversational/operational modifiers to extract the actual search term
        clutter_patterns = [
            "open ", "and put ", "and play ", "and watch ", "and find ", "and click ",
            "put a good ", "play a good ", "watch a good ", "find a good ",
            "put a ", "play a ", "watch a ", "find a ", "click a ", "click the ",
            "the ", "good ", "first ", "a ", "an ", "to ", "for ",
            "make it ", "make ", "search for ", "search "
        ]
        for pattern in clutter_patterns:
            query = query.replace(pattern, " ")
        
        # Clean up and normalize
        query = query.replace("  ", " ").strip()
        
        # Remove trailing "video", "videos", "thumbnail"
        for suffix in [" video", " videos", " thumbnail", " thumbnails"]:
            if query.endswith(suffix):
                query = query[:-len(suffix)].strip()
        
        query = query.strip()
        if not query:
            # Extract any meaningful word sequence as fallback
            words = prompt_lower.replace("youtube", "").replace("video", "").split()
            meaningful_words = [w for w in words if len(w) > 2 and w not in ["the", "and", "for", "put", "open", "good"]]
            query = " ".join(meaningful_words[:3]) if meaningful_words else "pewdiepie"
        
        # Handle common typos
        typo_fixes = {
            "markpleir": "markiplier",
            "markplier": "markiplier", 
            "peewdiepie": "pewdiepie",
            "pewdipie": "pewdiepie"
        }
        for typo, correct in typo_fixes.items():
            query = query.replace(typo, correct)
        
        print(f"[🔍 SEARCH EXTRACT] Extracted query from '{prompt_lower}' -> '{query}'")
        
        # Rebuild clean step execution sequence - always include click step for video tasks
        fallback_steps = ["launch chrome", "go to youtube.com", f"search for {query} on youtube", "click first video"]
        if "fullscreen" in prompt_lower or "full screen" in prompt_lower:
            fallback_steps.append("press f")
            
    elif "search" in prompt_lower or "find" in prompt_lower:
        fallback_steps = ["launch chrome", "focus address bar"]
        query = prompt_lower
        
        # Use word-boundary regex to avoid breaking words like "searching" -> "ing"
        for prefix in ["search for", "search", "find", "find me"]:
            query = re.sub(r'\b' + re.escape(prefix) + r'\b', "", query, count=1).strip()
        
        # Clean up search query
        query = query.replace("online", "").replace("on the internet", "").strip()
        
        # Handle vague "find a website and open it" type queries
        vague_website_queries = ["a website", "website", "a site", "site", "some website", "any website", "some site"]
        if query.lower().strip() in vague_website_queries or any(query.lower().strip() == q for q in vague_website_queries):
            # For vague queries, just go to google.com and let the user search
            print(f"[📋 FALLBACK] Vague website query detected: '{prompt_lower}' -> using google.com")
            return _clean_plan([
                "launch chrome",
                "focus address bar",
                "type google.com",
                "press enter",
                "wait 2.0"
            ], prompt)
        
        # Extract the actual search query by removing action descriptions
        # This handles: "search minecraft and go to official site" -> search="minecraft", action="go to official site"
        action_patterns = [
            "and tap ", "and click ", "and open ", "and visit ", "and go to ",
            "then tap ", "then click ", "then open ", "then visit ", "then go to ",
            "tap on ", "click on ", "open the ", "visit the ", "go to the ",
            "tap ", "click ", "open ", "visit ", "go to "
        ]
        
        search_query = query
        action_after_search = ""
        for pattern in action_patterns:
            if pattern in search_query:
                parts = search_query.split(pattern, 1)
                search_query = parts[0].strip()
                action_after_search = parts[1].strip() if len(parts) > 1 else ""
                break
        
        # Clean up search query
        search_query = search_query.replace("and ", "").replace("then ", "").strip()
        if not search_query:
            search_query = query.replace("and ", "").replace("then ", "").strip()
        
        # Determine if user wants to click/visit a result
        wants_click = bool(action_after_search) or any(kw in prompt_lower for kw in [
            "tap a website", "click a website", "open a website", "visit a website", 
            "tap website", "click website", "open website", "visit website", 
            "tap result", "click result", "open result", "visit result",
            "tap on", "click on", "tap the", "click the"
        ])
        
        # Handle common patterns
        if ("open google" in prompt_lower or "go to google" in prompt_lower):
            # Extract the intent phrase AFTER "open google" / "go to google"
            after = ""
            if "open google" in prompt_lower:
                after = prompt_lower.split("open google", 1)[1].strip()
            else:
                after = prompt_lower.split("go to google", 1)[1].strip()

            # Remove leading "and" / "then" / "to" connectors without leaving junk
            after = re.sub(r"^(and\s+|then\s+|to\s+)+", "", after).strip()

            # Decide intent
            has_find = any(kw in after for kw in ["find ", "search ", "look for ", "ways to"])
            wants_click = any(kw in prompt_lower for kw in [
                "tap on", "click on", "tap ", "click ", "tap a", "click a", 
                "open a", "visit a", "tap website", "click website", "open website", 
                "visit website", "tap result", "click result", "open result", "visit result",
                "go to ", "visit "
            ])

            # Case A: open Google AND do an action on Google
            if has_find or "search" in prompt_lower or "find" in prompt_lower:
                query = after
                for prefix in ["find ", "search for", "search", "find me", "look for", "ways to", "a way to", "ways of"]:
                    query = query.replace(prefix, "", 1).strip()
                query = query.replace("online", "").replace("on the internet", "").strip()
                query = re.sub(r"\s+", " ", query).strip()
                
                # Extract clean search query and action
                search_query = query
                action_after_search = ""
                for pattern in action_patterns:
                    if pattern in search_query:
                        parts = search_query.split(pattern, 1)
                        search_query = parts[0].strip()
                        action_after_search = parts[1].strip() if len(parts) > 1 else ""
                        break
                search_query = search_query.replace("and ", "").replace("then ", "").strip()
                
                if not search_query:
                    search_query = "minecraft"

                fallback_steps = [
                    "launch chrome",
                    "focus address bar",
                    "type google.com",
                    "press enter",
                    "wait 2.0",
                    f"type {search_query}",
                    "press enter",
                    "wait 2.0",
                ]
                if wants_click or action_after_search:
                    fallback_steps.append("click first search result")
            else:
                # Pure "open Google" intent
                fallback_steps = [
                    "launch chrome",
                    "focus address bar",
                    "type google.com",
                    "press enter",
                    "wait 2.0",
                ]
                if wants_click or action_after_search:
                    fallback_steps.append("click first search result")
        elif "go to" in prompt_lower or "open " in prompt_lower:
            target = prompt_lower
            for prefix in ["go to ", "open ", "navigate to "]:
                target = target.replace(prefix, "", 1).strip()
            target = target.split(" and ")[0].strip()
            fallback_steps = ["launch chrome", "focus address bar", f"type {target}", "press enter"]
            if wants_website_click or action_after_search:
                fallback_steps.extend(["wait 2.0", "click first search result"])
        else:
            if search_query:
                fallback_steps.append(f"type {search_query}")
            fallback_steps.append("press enter")
            if wants_click or action_after_search:
                fallback_steps.extend(["wait 2.0", "click first search result"])
        
    elif "antigravity" in prompt_lower or ("project" in prompt_lower and "vscode" not in prompt_lower):
        if "website" in prompt_lower:
            fallback_steps = ["launch antigravity", "create website project in antigravity"]
        else:
            fallback_steps = ["launch antigravity", "create new project in antigravity"]
            
    elif "vscode" in prompt_lower or "visual studio code" in prompt_lower:
        if "website" in prompt_lower:
            fallback_steps = ["launch vscode", "create website project in vscode"]
        else:
            fallback_steps = ["launch vscode", "create new project in vscode"]
            
    elif "download" in prompt_lower or "install" in prompt_lower:
        target = prompt_lower.replace("download", "").replace("install", "").replace("and", "").strip()
        if "epic" in prompt_lower:
            fallback_steps = ["launch chrome", "HOTKEY ctrl l", "TYPE epicgames.com", "PRESS enter", "WAIT 5.0", "CLICK download"]
        elif target:
            fallback_steps = ["launch chrome", "focus address bar", f"type {target} download", "press enter", "wait 2.0", "click first search result"]
    
    elif "learn python" in prompt_lower or "learning python" in prompt_lower or "python tutorial" in prompt_lower or "python course" in prompt_lower or "python site" in prompt_lower or ("open" in prompt_lower and "python" in prompt_lower and "learning" in prompt_lower):
        # Direct navigation to reputable Python learning resources
        python_sites = {
            "python.org": "python.org",
            "real python": "realpython.com",
            "w3schools": "w3schools.com/python",
            "codecademy": "codecademy.com/learn/learn-python-3",
            "coursera": "coursera.org/specializations/python",
            "udemy": "udemy.com/course/complete-python-bootcamp/",
        }
        
        # Pick the most appropriate site based on keywords
        target_site = "python.org"  # Default
        for keyword, site in python_sites.items():
            if keyword in prompt_lower:
                target_site = site
                break
        
        print(f"[📋 FALLBACK] Python learning request detected: {prompt_lower} -> {target_site}")
        fallback_steps = ["launch chrome", "focus address bar", f"type {target_site}", "press enter", "wait 2.0"]
    
    elif "open a website" in prompt_lower or "open website" in prompt_lower or "open a site" in prompt_lower or ("website" in prompt_lower and "open" in prompt_lower):
        # Generic "open a website" with optional topic - navigate to google and search
        query = prompt_lower.replace("open a website", "").replace("open website", "").replace("open a site", "").replace("open", "").strip()
        if query:
            fallback_steps = ["launch chrome", "focus address bar", f"type google.com", "press enter", "wait 2.0", f"type {query}", "press enter", "wait 2.0", "click first search result"]
        else:
            fallback_steps = ["launch chrome", "focus address bar", "type google.com", "press enter", "wait 2.0"]
    
    elif "file explorer" in prompt_lower or "explorer" in prompt_lower:
        print(f"[📋 FALLBACK] File explorer task detected: {prompt_lower}")
        if "desktop" in prompt_lower:
            fallback_steps = ["LAUNCH explorer", "FIND_TEXT desktop", "CLICK desktop"]
        else:
            fallback_steps = ["LAUNCH explorer"]
        if "new file" in prompt_lower or "make a file" in prompt_lower or "create file" in prompt_lower:
            # Extract filename if provided, otherwise use default
            filename = "new_file.txt"
            for keyword in ["new file", "make a file", "create file", "file named", "named "]:
                if keyword in prompt_lower:
                    remaining = prompt_lower.split(keyword, 1)[1].strip()
                    if remaining:
                        filename = remaining.split()[0]
                        # Add extension if missing
                        if "." not in filename:
                            filename += ".txt"
                    break
            # Use full desktop path so CREATE_FILE works regardless of current directory
            desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
            fallback_steps.append(f"CREATE_FILE {os.path.join(desktop_path, filename)}")
        print(f"[📋 FALLBACK] File explorer steps: {fallback_steps}")
    
    return fallback_steps

def run_chat_gui():
    """Launch a modern chat GUI with conversation history and screen analysis."""
    import tkinter as tk
    from tkinter import scrolledtext
    import threading
    import queue
    import time
    import datetime
    
    pyautogui.FAILSAFE = True
    
    root = tk.Tk()
    root.title("MaxAI Chat - Screen Automation Assistant")
    root.geometry("1100x600")
    root.configure(bg="#0d0d14")
    
    root.grid_rowconfigure(0, weight=1)
    root.grid_columnconfigure(1, weight=1)
    
    sidebar = tk.Frame(root, bg="#11111b", width=260)
    sidebar.grid(row=0, column=0, sticky="nsw")
    sidebar.grid_propagate(False)
    
    sidebar_header = tk.Frame(sidebar, bg="#1a1a2e", height=50)
    sidebar_header.pack(fill="x")
    sidebar_header.pack_propagate(False)
    
    tk.Label(sidebar_header, text="💬 Chats", bg="#1a1a2e", fg="#a6e3a8", font=("Segoe UI", 12, "bold")).pack(side="left", padx=12, pady=10)
    
    new_chat_btn = tk.Button(sidebar_header, text="+ New", bg="#89b4fa", fg="#1e1e2e",
                             font=("Segoe UI", 9, "bold"), relief="flat", padx=10,
                             command=lambda: new_chat())
    new_chat_btn.pack(side="right", padx=8)
    
    chat_list_frame = tk.Frame(sidebar, bg="#11111b")
    chat_list_frame.pack(fill="both", expand=True)
    
    chat_listbox = tk.Listbox(chat_list_frame, bg="#11111b", fg="#c6d0f5",
                              font=("Segoe UI", 10), relief="flat", borderwidth=0,
                              highlightthickness=0, activestyle="none",
                              selectbackground="#313244", selectforeground="#a6e3a8")
    chat_listbox.pack(fill="both", expand=True, padx=8, pady=8)
    
    def load_chat_list():
        chat_listbox.delete(0, tk.END)
        sessions = _list_chat_sessions()
        for session in sessions:
            title = session.get("title") or session["id"]
            chat_listbox.insert(tk.END, title)
            chat_listbox.itemconfig(tk.END, foreground="#c6d0f5")
    
    def on_chat_select(event):
        selection = chat_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        sessions = _list_chat_sessions()
        if 0 <= index < len(sessions):
            session = sessions[index]
            switch_chat(session["id"])
    
    def switch_chat(chat_id):
        global _current_chat_id
        _current_chat_id = chat_id
        _load_conversation_history(chat_id)
        render_history()
        status_label.config(text=f"● Chat: {chat_id}", fg="#89b4fa")
    
    def new_chat():
        chat_id = f"chat_{int(time.time())}"
        global _current_chat_id
        _current_chat_id = chat_id
        _conversation_history.clear()
        _save_conversation_history(title="New Chat")
        chat_display.config(state=tk.NORMAL)
        chat_display.delete("1.0", tk.END)
        chat_display.config(state=tk.DISABLED)
        load_chat_list()
        status_label.config(text="● Ready", fg="#89b4fa")
    
    def render_history():
        chat_display.config(state=tk.NORMAL)
        chat_display.delete("1.0", tk.END)
        for entry in _conversation_history:
            role = entry.get("role", "AI")
            text = entry.get("text", "")
            if role == "You":
                prefix = "You"
                tag = "user"
            elif role == "System":
                prefix = "System"
                tag = "error"
            else:
                prefix = "AI"
                tag = "ai"
            chat_display.insert(tk.END, f"{prefix}: {text}\n\n", tag)
        chat_display.tag_config("user", foreground="#a6e3a8", font=("Consolas", 11, "bold"))
        chat_display.tag_config("ai", foreground="#89b4fa", font=("Consolas", 11))
        chat_display.tag_config("error", foreground="#f9e2af", font=("Consolas", 11))
        chat_display.see(tk.END)
        chat_display.config(state=tk.DISABLED)
    
    chat_listbox.bind("<<ListboxSelect>>", on_chat_select)
    load_chat_list()
    
    main = tk.Frame(root, bg="#0d0d14")
    main.grid(row=0, column=1, sticky="nsew")
    main.grid_rowconfigure(1, weight=1)
    main.grid_columnconfigure(0, weight=1)
    
    header = tk.Frame(main, bg="#1a1a2e", height=60)
    header.grid(row=0, column=0, sticky="ew")
    header.grid_propagate(False)
    
    title_label = tk.Label(header, text="🤖 MaxAI Remote Agent", 
                          bg="#1a1a2e", fg="#a6e3a8", font=("Segoe UI", 16, "bold"))
    title_label.pack(side="left", padx=15, pady=10)
    
    status_label = tk.Label(header, text="● Ready", 
                           bg="#1a1a2e", fg="#89b4fa", font=("Segoe UI", 10))
    status_label.pack(side="right", padx=15)
    
    chat_frame = tk.Frame(main, bg="#0d0d14")
    chat_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
    chat_frame.grid_rowconfigure(0, weight=1)
    chat_frame.grid_columnconfigure(0, weight=1)
    
    chat_display = scrolledtext.ScrolledText(chat_frame, wrap=tk.WORD, 
                                            bg="#141423", fg="#c6d0f5", 
                                            font=("Consolas", 11),
                                            relief="flat", borderwidth=0)
    chat_display.grid(row=0, column=0, sticky="nsew")
    chat_display.config(state=tk.DISABLED)
    
    message_queue = queue.Queue()
    
    def _process_message_queue():
        try:
            while True:
                item = message_queue.get_nowait()
                chat_display.config(state=tk.NORMAL)
                tag = "error" if item.get("is_error") else ("user" if item.get("sender") == "You" else "ai")
                chat_display.insert(tk.END, f"{item['sender']}: {item['text']}\n\n", tag)
                chat_display.tag_config("user", foreground="#a6e3a8", font=("Consolas", 11, "bold"))
                chat_display.tag_config("ai", foreground="#89b4fa", font=("Consolas", 11))
                chat_display.tag_config("error", foreground="#f9e2af", font=("Consolas", 11))
                chat_display.see(tk.END)
                chat_display.config(state=tk.DISABLED)
        except queue.Empty:
            pass
        root.after(50, _process_message_queue)
    
    root.after(50, _process_message_queue)
    
    input_frame = tk.Frame(main, bg="#0d0d14", height=80)
    input_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
    input_frame.grid_propagate(False)
    input_frame.grid_columnconfigure(0, weight=1)
    
    prompt_var = tk.StringVar()
    entry = tk.Entry(input_frame, textvariable=prompt_var, font=("Segoe UI", 12),
                    bg="#1e1e2e", fg="#a6e3a8", insertbackground="#a6e3a8",
                    relief="flat")
    entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
    entry.focus()
    
    send_btn = tk.Button(input_frame, text="Send", 
                        bg="#89b4fa", fg="#1e1e2e", font=("Segoe UI", 11, "bold"),
                        relief="flat", padx=20)
    send_btn.grid(row=0, column=1)
    
    def append_message(sender, text, is_error=False):
        message_queue.put({
            "sender": sender,
            "text": text,
            "is_error": is_error
        })

    def step_description(step):
        """Convert a raw automation step to a concise human-readable description."""
        s = step.strip().strip("'\"")
        s_lower = s.lower()
        if s_lower.startswith("launch "):
            target = s_lower.split(" ", 1)[1].strip()
            return f"Opening {target.title()}..."
        if s_lower.startswith("hotkey "):
            keys = s_lower.split(" ", 1)[1].strip()
            mapping = {
                "ctrl l": "Focusing address bar...",
                "ctrl t": "Opening new tab...",
                "ctrl w": "Closing tab...",
                "ctrl r": "Refreshing page...",
                "alt left": "Going back...",
                "alt right": "Going forward...",
                "ctrl ,": "Opening VS Code settings..."
            }
            return mapping.get(keys, f"Pressing hotkey {keys}...")
        if s_lower.startswith("press "):
            key = s_lower.split(" ", 1)[1].strip().strip("'\"")
            return f"Pressing {key}..."
        if s_lower.startswith("type "):
            text = s_lower.split(" ", 1)[1].strip().strip("'\"")
            return f"Typing: {text[:40]}..."
        if s_lower.startswith("type_in "):
            text = s_lower.split(" ", 1)[1].strip().strip("'\"")
            return f"Filling in: {text[:40]}..."
        if s_lower.startswith("wait "):
            return "Waiting..."
        if s_lower.startswith("click "):
            target = s_lower.split(" ", 1)[1].strip().strip("'\"")
            return f"Clicking {target}..."
        if s_lower == "select_profile":
            return "Selecting Chrome profile..."
        if s_lower.startswith("find_text "):
            return "Looking for text on screen..."
        if s_lower.startswith("find_image "):
            return "Looking for image on screen..."
        if s_lower.startswith("focus_app "):
            app = s_lower.split(" ", 1)[1].strip()
            return f"Focusing {app}..."
        if s_lower.startswith("browser_action "):
            action = s_lower.split(" ", 1)[1].strip()
            return f"Browser action: {action}..."
        if s_lower.startswith("screen_analyze"):
            return "Analyzing screen..."
        if s_lower.startswith("chat_reply"):
            return "Generating chat reply..."
        if s_lower.startswith("scroll "):
            return "Scrolling..."
        if s_lower.startswith("tab "):
            return "Tabbing..."
        if s_lower.startswith("move_to ") or s_lower.startswith("move_rel "):
            return "Moving mouse..."
        if s_lower.startswith("create_file ") or s_lower.startswith("write_file "):
            return "Creating/writing file..."
        if s_lower.startswith("run_file "):
            return "Running file..."
        if s_lower.startswith("vscode_action "):
            action = s_lower.split(" ", 1)[1].strip()
            return f"VS Code action: {action}..."
        if s_lower.startswith("antigravity_action "):
            action = s_lower.split(" ", 1)[1].strip()
            return f"Antigravity action: {action}..."
        if s_lower == "self_correct":
            return "Self-correcting..."
        if s_lower == "expect":
            return "Setting expectation..."
        if s_lower.startswith("create_project ") or s_lower == "create_project":
            return "Creating project..."
        if s_lower.startswith("write_in_code "):
            return "Writing code in editor..."
        if s_lower.startswith("open_file_explorer "):
            return "Opening file explorer..."
        return s

    def get_screen_analysis():
         context = {"workspace": os.getcwd()}
         detect_screen_context(context)
         screen_info = context.get('screen_summary', 'Could not analyze screen')
         return context, screen_info
 
    def process_task(task_str):
        status_label.config(text="● Working...", fg="#f9e2af")
        append_message("You", task_str)
        user_meta = {}

        waiting_enabled = True
        task_lower = task_str.lower().strip()
        if "waiting false" in task_lower or "waiting off" in task_lower:
            waiting_enabled = False
            set_skip_waits(True)
            append_message("AI", "Got it — I'll skip all waits for this task.")
            _add_conversation("AI", "Got it — I'll skip all waits for this task.", meta={
                "window": context.get("screen_active_window", ""),
                "screen_summary": context.get("screen_summary", ""),
                "type": "wait_override"
            })
            for prefix in ["waiting false", "waiting off"]:
                if task_lower.startswith(prefix):
                    task_str = task_str[len(prefix):].strip()
                    task_lower = task_str.lower().strip()
                    break

        context, _ = get_screen_analysis()
        user_meta["window"] = context.get("screen_active_window", "")
        user_meta["screen_summary"] = context.get("screen_summary", "")
        user_meta["is_browser"] = context.get("screen_is_browser", False)
        user_meta["is_ide"] = context.get("screen_is_ide", False)
        _add_conversation("You", task_str, meta=user_meta)
        
        if len(_conversation_history) == 1:
            _save_conversation_history(title=task_str[:50])
            load_chat_list()

        confirmation_words = ["yes", "yeah", "yep", "sure", "ok", "okay", "proceed", "go ahead", "do it", "please do", "please proceed", "confirm", "confirmed", "affirmative", "yup", "ya", "yeh"]
        if task_lower.strip().strip(".!?,") in confirmation_words or any(task_lower.startswith(w) for w in confirmation_words):
            pending_task = _get_and_clear_pending_verification()
            if pending_task:
                append_message("AI", f"Executing: {pending_task}")
                _add_conversation("AI", f"Executing: {pending_task}", meta={
                    "window": context.get("screen_active_window", ""),
                    "screen_summary": context.get("screen_summary", ""),
                    "type": "pending_execution"
                })
                task_str = pending_task
                task_lower = task_str.lower().strip()

        try:
            sub_tasks = decompose_task(task_str, context)
            if not sub_tasks:
                append_message("AI", "I couldn't figure out how to do that. Want me to search for instructions instead?")
                _add_conversation("AI", "I couldn't figure out how to do that. Want me to search for instructions instead?", meta={
                    "window": context.get("screen_active_window", ""),
                    "screen_summary": context.get("screen_summary", ""),
                    "type": "decompose_failed"
                })
                status_label.config(text="● Ready", fg="#89b4fa")
                return

            exec_context = {"workspace": os.getcwd(), "retry_count": 0, "skip_waits": not waiting_enabled, "_retry_attempt": 0}

            for task_idx, sub_prompt in enumerate(sub_tasks):
                if _stop_requested:
                    append_message("AI", "Stopped mid-task.")
                    _add_conversation("AI", "Stopped mid-task.", meta={
                        "window": context.get("screen_active_window", ""),
                        "screen_summary": context.get("screen_summary", ""),
                        "type": "stopped"
                    })
                    break

                ai_meta = {
                    "window": context.get("screen_active_window", ""),
                    "screen_summary": context.get("screen_summary", ""),
                    "is_browser": context.get("screen_is_browser", False),
                    "is_ide": context.get("screen_is_ide", False),
                    "sub_task": sub_prompt
                }

                sp = sub_prompt.lower().strip()
                rule_based_plan = None

                if sp == "launch chrome":
                    rule_based_plan = ["LAUNCH chrome", "SELECT_PROFILE"]
                elif sp.startswith("launch ") or sp.startswith("open "):
                    app_name = sp.replace("launch ", "").replace("open ", "").strip()
                    app_name = app_name.title()
                    rule_based_plan = ["PRESS win", f"TYPE {app_name}", "WAIT 1.5", "PRESS enter", "WAIT 2.0"]
                    if "epic" in sp:
                        rule_based_plan = ["PRESS win", "TYPE Epic Games", "WAIT 1.5", "PRESS enter", "WAIT 3.0"]
                elif sp in ["go to youtube.com", "open youtube", "go to youtube"]:
                    rule_based_plan = ["HOTKEY ctrl l", "TYPE youtube.com", "PRESS enter", "WAIT 3.0"]
                elif "search for" in sp and "youtube" in sp:
                    query = sp.replace("search for", "").replace("youtube", "").strip()
                    query = query.replace(" and click it", "").replace(" on ", " ").replace(" and put ", " ").strip()
                    query = query.replace("open yotube", "").replace("go to yotube", "").strip()
                    for prefix in ["open ", "go ", "a ", "the ", "good "]:
                        query = query.strip()
                        if query.startswith(prefix):
                            query = query[len(prefix):].strip()
                    if not query:
                        query = "pewdiepie"
                    if "markpleir" in query or "markplier" in query:
                        query = query.replace("markpleir", "markiplier").replace("markplier", "markiplier")
                    rule_based_plan = ["PRESS /", f"TYPE {query}", "PRESS enter", "WAIT 2.0"]
                elif "click" in sp and "video" in sp:
                    query = sp.replace("click", "").replace("first video", "").replace("video", "").strip()
                    if not query or "first" in query:
                        rule_based_plan = ["CLICK video"]
                    else:
                        rule_based_plan = [f"FIND_TEXT {query}", "PRESS enter"]
                elif "click" in sp and "search result" in sp:
                    rule_based_plan = ["CLICK first search result"]
                elif "tap" in sp and "website" in sp:
                    rule_based_plan = ["CLICK first search result"]
                elif sp == "press f" or sp == "make it fullscreen" or sp == "make it full screen" or "fullscreen" in sp:
                    rule_based_plan = ["PRESS f"]
                elif sp == "focus address bar" or sp == "focus the address bar":
                    rule_based_plan = ["HOTKEY ctrl l"]
                elif "type" in sp and "youtube" in sp:
                    rule_based_plan = ["PRESS /", f"TYPE {sp.replace('type', '').replace('on youtube', '').replace('youtube', '').strip()}", "PRESS enter"]
                elif "create new project" in sp or "create website project" in sp:
                    if "antigravity" in sp:
                        rule_based_plan = ["CREATE_PROJECT my_project"]
                    elif "vscode" in sp or "visual studio code" in sp:
                        rule_based_plan = ["CREATE_PROJECT my_project"]
                    else:
                        rule_based_plan = ["CREATE_PROJECT my_project"]

                mini_plan = rule_based_plan

                if not mini_plan:
                    system_instruction = (
                        "You are a hyper-precise OS automation orchestrator.\n"
                        "Respond with ONLY a valid JSON array of step commands.\n\n"
                        "Commands:\n"
                        "- LAUNCH <app>, HOTKEY <keys>, PRESS <key>, TYPE \"text\", CLICK <element>, FIND_TEXT <text>\n"
                        "- WAIT <seconds>, FOCUS_APP <name>, SELECT_PROFILE, CREATE_FILE <path>\n"
                        "- BROWSER_ACTION <action>, SCROLL <amount>, TAB <count>\n\n"
                        "Plan for: " + sub_prompt + "\nonly JSON array output:"
                    )

                    response = ollama_chat(
                        model=OLLAMA_MODEL,
                        messages=[
                            {"role": "system", "content": system_instruction},
                            {"role": "user", "content": sub_prompt}
                        ],
                        temperature=0.0,
                        max_tokens=256
                    )

                    raw_response = response['choices'][0]['message']['content']
                    json_match = re.search(r'\[.*?\]', raw_response, re.DOTALL)
                    if json_match:
                        mini_plan, _ = _safe_json_loads(json_match.group())
                        if mini_plan is None:
                            mini_plan = [sub_prompt]
                    else:
                        mini_plan = [sub_prompt]
                    
                    mini_plan = _merge_split_commands(mini_plan)
                    mini_plan = [s for s in mini_plan if _is_valid_command(s)]
                    if not mini_plan:
                        append_message("AI", f"I couldn't generate a valid plan for: {sub_prompt}. Skipping this step.")
                        _add_conversation("AI", f"I couldn't generate a valid plan for: {sub_prompt}. Skipping this step.", meta={
                            "window": context.get("screen_active_window", ""),
                            "screen_summary": context.get("screen_summary", ""),
                            "sub_task": sub_prompt,
                            "type": "plan_failed"
                        })
                        continue

                for step_idx, step in enumerate(mini_plan):
                    if not isinstance(step, str):
                        print(f"[⚠️ STEP] Skipping non-string step: {step!r}")
                        continue
                    step = step.strip("'\"")
                    append_message("AI", step_description(step))
                    _add_conversation("AI", step_description(step), meta=ai_meta)
                    success, exec_context = execute_macro_step(step, exec_context)

                    if not success:
                        err = exec_context.get("last_error", "unknown") or "unknown"
                        if any(x in err for x in ["Unknown command", "Could not locate", "not found", "Unknown"]):
                            append_message("AI", "I hit something unexpected. Let me look at the screen and find another way.")
                            _add_conversation("AI", "I hit something unexpected. Let me look at the screen and find another way.", meta={
                                "window": context.get("screen_active_window", ""),
                                "screen_summary": context.get("screen_summary", ""),
                                "sub_task": sub_prompt,
                                "type": "fallback"
                            })
                            detect_screen_context(exec_context)
                            try:
                                fallback_reply = _generate_visual_chat_reply(f"How can I accomplish: {sub_prompt}", exec_context)
                                append_message("AI", fallback_reply)
                                _add_conversation("AI", fallback_reply, meta={
                                    "window": exec_context.get("screen_active_window", ""),
                                    "screen_summary": exec_context.get("screen_summary", ""),
                                    "type": "fallback_reply"
                                })
                            except Exception:
                                append_message("System", f"Couldn't do that step — {err[:60]}", is_error=True)
                                _add_conversation("System", f"Couldn't do that step — {err[:60]}", meta={
                                    "window": context.get("screen_active_window", ""),
                                    "screen_summary": context.get("screen_summary", ""),
                                    "sub_task": sub_prompt,
                                    "type": "error"
                                })
                            break
                        else:
                            append_message("System", f"Couldn't do that step — {err[:60]}", is_error=True)
                            _add_conversation("System", f"Couldn't do that step — {err[:60]}", meta={
                                "window": context.get("screen_active_window", ""),
                                "screen_summary": context.get("screen_summary", ""),
                                "sub_task": sub_prompt,
                                "type": "error"
                            })

                # brief rest between sub-tasks
                if waiting_enabled:
                    time.sleep(0.5)

            if not _stop_requested:
                append_message("AI", "Task complete.")
                _add_conversation("AI", "Task complete.", meta={
                    "window": context.get("screen_active_window", ""),
                    "screen_summary": context.get("screen_summary", ""),
                    "steps": sub_tasks
                })
                load_chat_list()
        except ChatReply as cr:
            reply_text = cr.reply_text
            append_message("AI", reply_text)
            _add_conversation("AI", reply_text, meta={
                "window": context.get("screen_active_window", ""),
                "screen_summary": context.get("screen_summary", ""),
                "type": "chat_reply"
            })
            embedded_actions = _extract_actions_from_reply(reply_text)
            if embedded_actions:
                append_message("AI", f"Executing: {', '.join(embedded_actions)}")
                _add_conversation("AI", f"Executing: {', '.join(embedded_actions)}", meta={
                    "window": context.get("screen_active_window", ""),
                    "screen_summary": context.get("screen_summary", ""),
                    "type": "embedded_action"
                })
                def _execute_embedded():
                    exec_ctx = {"workspace": os.getcwd(), "retry_count": 0, "skip_waits": not waiting_enabled, "_retry_attempt": 0}
                    for action in embedded_actions:
                        if _stop_requested:
                            break
                        success, exec_ctx = execute_macro_step(action, exec_ctx)
                        if not success:
                            append_message("System", f"Couldn't do embedded action — {exec_ctx.get('last_error', 'unknown')[:60]}", is_error=True)
                            _add_conversation("System", f"Couldn't do embedded action — {exec_ctx.get('last_error', 'unknown')[:60]}", meta={
                                "window": context.get("screen_active_window", ""),
                                "screen_summary": context.get("screen_summary", ""),
                                "type": "embedded_action_error"
                            })
                            break
                threading.Thread(target=_execute_embedded, daemon=True).start()
            if any(kw in reply_text.lower() for kw in ["would you like me to proceed", "would you like me to", "shall i proceed", "shall i", "confirm", "verify", "is that correct", "is this correct", "proceed with", "go ahead"]):
                _set_pending_verification(task_str)
            elif any(v in task_str.lower() for v in ["open", "launch", "close", "click", "type", "search", "find", "go", "make", "create", "build", "run", "execute", "start", "stop", "wait", "focus", "save", "delete", "write", "read", "scroll", "press", "hotkey", "navigate", "select", "switch", "install", "download", "watch", "play"]):
                _set_pending_verification(task_str)
        except Exception as e:
            append_message("System", f"Error: {str(e)[:120]}", is_error=True)
            _add_conversation("System", f"Error: {str(e)[:120]}", meta={
                "window": context.get("screen_active_window", ""),
                "error": str(e)[:120]
            })

        status_label.config(text="● Ready", fg="#89b4fa")
    
    def on_send():
        task = prompt_var.get().strip()
        if task:
            prompt_var.set("")
            if len(_conversation_history) == 0:
                _save_conversation_history(title=task[:50])
                load_chat_list()
            threading.Thread(target=process_task, args=(task,), daemon=True).start()
    
    entry.bind("<Return>", lambda e: on_send())
    send_btn.config(command=on_send)
    
    sessions = _list_chat_sessions()
    if sessions:
        switch_chat(sessions[0]["id"])
    else:
        new_chat()
    
    root.mainloop()

def main():
    pyautogui.FAILSAFE = True
    print("\n=== 100% OFFLINE LOCAL AUTOMATION MATRIX ===")
    user_macro_goal = input("\nWhat local task sequence should be built? ")
    
    # Parse WAITING flag
    waiting_enabled = True
    goal_lower = user_macro_goal.lower().strip()
    if "waiting false" in goal_lower or "waiting off" in goal_lower:
        waiting_enabled = False
        # Remove the waiting flag from the prompt
        for prefix in ["waiting false", "waiting off"]:
            if goal_lower.startswith(prefix):
                user_macro_goal = user_macro_goal[len(prefix):].strip()
                break
        set_skip_waits(True)
        print("[⚡ WAITING] FALSE detected - all WAIT commands will be skipped")
    elif "waiting true" in goal_lower or "waiting on" in goal_lower:
        waiting_enabled = True
        for prefix in ["waiting true", "waiting on"]:
            if goal_lower.startswith(prefix):
                user_macro_goal = user_macro_goal[len(prefix):].strip()
                break
        set_skip_waits(False)
        print("[⚡ WAITING] TRUE detected - WAIT commands enabled")
    else:
        set_skip_waits(False)
    system_instruction = (
    "You are a hyper-precise OS automation orchestrator with full system visibility.\n"
    "Your goal is 100% reliable task execution.\n\n"
    "SYSTEM UNDERSTANDING:\n"
    "- The system can see all open windows, their titles, and contents via UI Automation (uiautomation)\n"
    "- You have access to keyboard shortcuts, direct file operations, and visual detection\n\n"
    "CRITICAL RULES FOR 100% ACCURACY:\n"
             "1. YOUTUBE SEARCH OVERRIDE (HIGHEST PRIORITY): When on YouTube, ALWAYS use 'PRESS /' to focus YouTube's search bar, then TYPE the query, then PRESS enter. NEVER use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address' for YouTube. '/' is YouTube's own search shortcut and must be used INSTEAD of address-bar shortcuts.\n"
             "2. CURRENT WINDOW FIRST: If the user's task is about what's currently on screen (chat, forms, editors, browsers), ALWAYS work in the ACTIVE WINDOW. Do NOT launch new apps unless explicitly asked.\n"
             "3. SCREEN ANALYSIS BEFORE ACTION: Run SCREEN_ANALYZE at the start of EVERY task to understand context. Use the visible text and window title to guide your plan.\n"
             "4. DISCORD/CHAT APPS: For Discord, Slack, Teams, Telegram, or any chat app: use FIND_TEXT to read messages, SCROLL to navigate history, CHAT_REPLY to generate and send a contextual reply (preferred), or TYPE to reply manually. NEVER open a new window for chat tasks.\n"
             "5. KEYBOARD SHORTCUTS > CLICKING ALWAYS. Never try to visually locate UI elements when a keyboard shortcut exists.\n"
             "6. For general browser address bar (NOT YouTube): use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address', NEVER look for 'address_bar' visually.\n"
             "7. CHROME PROFILE: After LAUNCH chrome, you MUST add 'SELECT_PROFILE' to handle the profile picker screen. The system auto-injects this, but explicitly include it.\n"
             "8. WINDOW FOCUS: If an action fails because a window is hidden, use 'FOCUS_APP <app_name>' first.\n"
             "9. If a step fails, the system will automatically retry with corrected steps using SELF_CORRECT.\n"
             "10. Only use visual detection (CLICK/DOUBLE_CLICK) when no keyboard shortcut exists.\n"
             "11. WAIT COMMANDS: Use WAIT after actions that need time to load. BUT if the task prompt contains 'WAITING FALSE', DO NOT include any WAIT commands - execute steps back-to-back.\n"
             "12. OPENING PROGRAMS (EXCEPT CHROME): For VS Code, Antigravity, and all other apps except Chrome, use Windows search: 'PRESS win', 'WAIT 1.0', 'TYPE \"<app_name>\"', 'WAIT 1.5', 'PRESS enter'. FOR CHROME: use ONLY 'LAUNCH chrome'.\n"
             "13. CLICK STRATEGY: For clicking, use descriptive semantic labels matching visible text. NEVER use numbers like 'CLICK 1' or generic labels. Use the exact text visible on the UI element.\n"
             "14. SEARCH IN BROWSER: FOR YOUTUBE SEARCH: use 'PRESS '/'', TYPE query, PRESS enter. FOR OTHER BROWSERS: use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address'.\n"
              "15. CLICK VIDEOS: To click a YouTube video, ALWAYS use 'CLICK video'. This triggers thumbnail-based detection that finds the actual video thumbnail on screen. Do NOT use 'FIND_TEXT' for videos - it will click on text labels instead of the video.\n"
              "16. FULL SCREEN VIDEO: For full screen on YouTube, use 'PRESS F'. For other websites, use 'PRESS F11'. Or use 'FIND_TEXT \"Full screen\"' then 'CLICK \"Full screen\"'.\n"
              "17. EXPECTATIONS: Use EXPECT to verify outcomes: 'EXPECT <element> text_contains_any \"YouTube,Video\"' or 'EXPECT <element> window_title \"YouTube\"'.\n"
              "18. FILE CREATION: For desktop file creation, ALWAYS use CREATE_FILE directly. Never navigate file picker dialogs - use direct OS operations.\n"
              "19. VIDEO THUMBNAILS: For YouTube videos, use 'CLICK video' to click the first video thumbnail. The system uses Florence-2 vision model to detect video thumbnails by shape and content.\n\n"
             "20. CRITICAL: If screen_active_window is 'Program Manager', 'Desktop', or similar, NEVER use BROWSER_ACTION, HOTKEY ctrl l, or any browser shortcut. Instead use LAUNCH chrome to open the browser FIRST.\n\n"
              "21. TASK DECOMPOSITION: When given a complex multi-step task, break it into 3-8 granular steps where each step is ONE focused action like 'launch chrome', 'search for horror videos on youtube', 'click first video', 'press f'.\n\n"
     "AVAILABLE COMMANDS:\n"
     "- LAUNCH <app_name>  (ONLY for chrome; for other apps use Windows search sequence)\n"
     "- HOTKEY <key1> <key2> ...  (preferred over CLICK - e.g., 'HOTKEY ctrl l' for address bar)\n"
     "- PRESS <key_name>  (single key press)\n"
     "- TYPE \"<text>\"  (type text at current cursor location)\n"
     "- TYPE_IN <element> \"<text>\"  (click element then type - only when no shortcut)\n"
     "- CLICK <semantic_element_label>  (describe what to click - match visible text)\n"
     "- FIND_TEXT <text>  (find text on screen via OCR and move mouse there)\n"
     "- SCREEN_ANALYZE  (scan current screen: active window, visible text, regions)\n"
     "- WAIT <seconds>  (pause execution - skip if WAITING FALSE is set)\n"
     "- FOCUS_APP <app_name>  (focus window by name)\n"
     "- SELECT_PROFILE  (MUST use after LAUNCH chrome for profile picker)\n"
     "- BROWSER_ACTION <action_name>  (focus_address, new_tab, close_tab, refresh, back, forward, home)\n"
     "- SELF_CORRECT  (auto-retries on failure with intelligent correction)\n\n"
      "EXAMPLES:\n"
      "YouTube horror video (decomposed plan):\n"
      "[\"launch chrome\", \"go to youtube.com\", \"PRESS /\", \"TYPE \\\"horror videos\\\"\", \"PRESS enter\", \"WAIT 3.0\", \"FIND_TEXT \\\"horror video\\\"\", \"CLICK \\\"horror video\\\"\", \"PRESS f\"]\n\n"
      "Search and click video:\n"
      "[\"launch chrome\", \"go to youtube.com\", \"PRESS /\", \"TYPE \\\"scary videos\\\"\", \"PRESS enter\", \"WAIT 3.0\", \"FIND_TEXT \\\"scary video\\\"\", \"CLICK \\\"scary video\\\"\", \"PRESS f\"]\n\n"
      "Google search and click result:\n"
      "[\"launch chrome\", \"focus address bar\", \"TYPE \\\"google.com\\\"\", \"PRESS enter\", \"WAIT 2.0\", \"TYPE \\\"minecraft\\\"\", \"PRESS enter\", \"WAIT 2.0\", \"CLICK \\\"official minecraft site\\\"\"]\n\n"
     "If you see 'Profile Picker' or 'Who's Using Chrome' window title, use SELECT_PROFILE.\n"
     "If element can't be found: run SCREEN_ANALYZE, try alternative labels, then SELF_CORRECT.\n\n"
     "Respond with ONLY a valid JSON array. NO PROSE, NO MARKDOWN, NO EXPLANATION. Just the raw array."
 )

    print("\n[*] TIER 1: Running Local text reasoning matrix...")
    try:
        # Step 0: Analyze screen
        initial_context = {"workspace": os.getcwd()}
        detect_screen_context(initial_context)
        screen_info = initial_context.get('screen_summary', 'No screen data')
        _add_conversation("You", user_macro_goal, meta={
            "window": initial_context.get("screen_active_window", ""),
            "screen_summary": initial_context.get("screen_summary", ""),
            "is_browser": initial_context.get("screen_is_browser", False),
            "is_ide": initial_context.get("screen_is_ide", False)
        })
        
        # Step 1: Decompose the task into smaller sub-tasks
        print("\n[*] TIER 1.5: Decomposing task into sub-tasks...")
        sub_tasks = decompose_task(user_macro_goal, initial_context)
        print(f"[📋 TASK BREAKDOWN] {len(sub_tasks)} sub-tasks: {sub_tasks}")
        
        is_youtube = initial_context.get('screen_is_youtube', False)
        
        # Step 2: Execute each sub-task with its own mini-plan
        all_steps = []
        context = {"workspace": os.getcwd(), "retry_count": 0, "skip_waits": not waiting_enabled, "_retry_attempt": 0}
        
        for task_idx, sub_prompt in enumerate(sub_tasks):
            border = "=" * 60
            print(f"\n{border}")
            print(f"[🎯 SUB-TASK {task_idx+1}/{len(sub_tasks)}] {sub_prompt}")
            print(f"[📌 FOCUS] Currently working on: {sub_prompt}")
            print(f"{border}")
            
            reason_overlay(f"[FOCUS {task_idx+1}/{len(sub_tasks)}]\n{sub_prompt}\n\n"
                          f"Waiting: {'OFF' if not waiting_enabled else 'ON'}")
            
            # Detect context for this sub-task
            detect_screen_context(context)
            is_youtube = context.get('screen_is_youtube', False)
            is_browser = context.get('screen_is_browser', False)
            
            if context.get("screen_summary"):
                print(f"[🖥️ SCREEN] {context['screen_summary'][:200]}")
            
            # Build mini-plan prompt
            waiting_note = "WAITING IS DISABLED - do NOT include WAIT commands." if not waiting_enabled else "Use WAIT commands for loading times."
            
            youtube_rule = ""
            if is_youtube:
                youtube_rule = "\nCRITICAL: You are on YouTube. Use 'PRESS /' to focus YouTube search bar. NEVER use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address'."
            elif is_browser:
                youtube_rule = "\nBROWSER RULE: You are in a browser (NOT YouTube). Use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address' to focus address bar."
            
            # ===== RULE-BASED MINI-PLAN =====
            sp = sub_prompt.lower().strip()
            rule_based_plan = None
            
            if sp == "launch chrome":
                rule_based_plan = ["LAUNCH chrome", "SELECT_PROFILE"]
            elif sp.startswith("launch ") or sp.startswith("open "):
                app_name = sp.replace("launch ", "").replace("open ", "").strip()
                app_name = app_name.title()
                rule_based_plan = ["PRESS win", f"TYPE {app_name}", "WAIT 1.5", "PRESS enter", "WAIT 2.0"]
                if "epic" in sp:
                    rule_based_plan = ["PRESS win", "TYPE Epic Games", "WAIT 1.5", "PRESS enter", "WAIT 3.0"]
            elif sp in ["go to youtube.com", "open youtube", "go to youtube"]:
                rule_based_plan = ["HOTKEY ctrl l", "TYPE youtube.com", "PRESS enter", "WAIT 3.0"]
            elif "search for" in sp and "youtube" in sp:
                query = sp.replace("search for", "").replace("youtube", "").strip()
                query = query.replace(" and click it", "").replace(" on ", " ").replace(" and put ", " ").strip()
                query = query.replace("open yotube", "").replace("go to yotube", "").strip()
                for prefix in ["open ", "go ", "a ", "the ", "good "]:
                    query = query.strip()
                    if query.startswith(prefix):
                        query = query[len(prefix):].strip()
                if not query:
                    query = "pewdiepie"
                # Fix common typos
                if "markpleir" in query or "markplier" in query:
                    query = query.replace("markpleir", "markiplier").replace("markplier", "markiplier")
                rule_based_plan = ["PRESS /", f"TYPE {query}", "PRESS enter", "WAIT 2.0"]
            elif "click" in sp and "video" in sp:
                # Always use thumbnail-based video clicking for YouTube
                rule_based_plan = ["CLICK video"]
            elif sp == "press f" or sp == "make it fullscreen" or sp == "make it full screen" or "fullscreen" in sp:
                rule_based_plan = ["PRESS f"]
            elif sp == "focus address bar" or sp == "focus the address bar":
                rule_based_plan = ["HOTKEY ctrl l"]
            elif "type" in sp and "youtube" in sp:
                rule_based_plan = ["PRESS /", f"TYPE {sp.replace('type', '').replace('on youtube', '').replace('youtube', '').strip()}", "PRESS enter"]
            elif any(x in sp for x in ["open and make website", "open the folder", "start building website", "open folder for website"]):
                rule_based_plan = ["WAIT 0.5"]
            
            # Antigravity and VS Code rules
            if not rule_based_plan:
                antigravity_already_open = context.get("screen_is_ide") and context.get("screen_ide_type") == "antigravity"
                vscode_already_open = context.get("screen_is_ide") and context.get("screen_ide_type") == "vscode"
                
                if sp == "launch antigravity":
                    if antigravity_already_open:
                        rule_based_plan = ["WAIT 0.5"]
                    else:
                        rule_based_plan = ["PRESS win", "TYPE Antigravity", "WAIT 1.0", "PRESS enter", "WAIT 2.0"]
                elif sp == "launch vscode":
                    if vscode_already_open:
                        rule_based_plan = ["WAIT 0.5"]
                    else:
                        rule_based_plan = ["PRESS win", "TYPE Visual Studio Code", "WAIT 1.0", "PRESS enter", "WAIT 2.0"]
                elif any(x in sp for x in ["open and make website", "open the folder", "start building website", "open folder for website"]):
                    rule_based_plan = ["WAIT 0.5"]
                elif ("create new project" in sp or "create website project" in sp or "make website project" in sp) and "antigravity" in sp:
                    project_name = sp.replace("create new project in antigravity", "").replace("create project in antigravity", "").replace("create website project in antigravity", "").replace("website project in antigravity", "").replace("make website project in antigravity", "").strip()
                    if not project_name:
                        if "website" in sp:
                            project_name = "my_website"
                        else:
                            project_name = "my_project"
                    if "website" in sp:
                        rule_based_plan = [f"CREATE_PROJECT {project_name}", f"WRITE_FILE {project_name}/index.html \"<!DOCTYPE html><html><head><title>{project_name}</title><link rel='stylesheet' href='style.css'></head><body><h1>Welcome to {project_name}</h1><script src='script.js'></script></body></html>\"", f"WRITE_FILE {project_name}/style.css \"body {{ font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; }} h1 {{ color: #333; }}\"", f"WRITE_FILE {project_name}/script.js \"console.log('{project_name} loaded');\""]
                    else:
                        rule_based_plan = [f"CREATE_PROJECT {project_name}"]
                elif "website" in sp and "antigravity" in sp:
                    rule_based_plan = ["CREATE_PROJECT my_website", "WRITE_FILE my_website/index.html \"<!DOCTYPE html><html><head><title>My Website</title><link rel='stylesheet' href='style.css'></head><body><h1>Welcome</h1><script src='script.js'></script></body></html>\"", "WRITE_FILE my_website/style.css \"body { font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; }\"", "WRITE_FILE my_website/script.js \"console.log('Website loaded');\""]
                elif "create new project" in sp and "vscode" in sp:
                    project_name = sp.replace("create new project in vscode", "").replace("create project in vscode", "").strip()
                    if not project_name:
                        project_name = "my_project"
                    rule_based_plan = [f"CREATE_PROJECT {project_name}"]
                elif "create website project" in sp and "vscode" in sp:
                    project_name = sp.replace("create website project in vscode", "").replace("website project in vscode", "").strip()
                    if not project_name:
                        project_name = "my_website"
                    rule_based_plan = [f"CREATE_PROJECT {project_name}", f"WRITE_FILE {project_name}/index.html \"<!DOCTYPE html><html><head><title>{project_name}</title><link rel='stylesheet' href='style.css'></head><body><h1>Welcome to {project_name}</h1><script src='script.js'></script></body></html>\"", f"WRITE_FILE {project_name}/style.css \"body {{ font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; }} h1 {{ color: #333; }}\"", f"WRITE_FILE {project_name}/script.js \"console.log('{project_name} loaded');\""]
                elif "make website" in sp or "create website" in sp:
                    project_name = sp.replace("make website", "").replace("create website", "").replace("for", "").replace("easy money", "").replace("platform", "").strip()
                    if not project_name:
                        project_name = "my_website"
                    if "easy money" in sp or "platform" in sp:
                        rule_based_plan = ["CREATE_PROJECT easy_money_platform", "WRITE_FILE easy_money_platform/index.html \"<!DOCTYPE html><html><head><title>Easy Money Platform</title><link rel='stylesheet' href='style.css'></head><body><h1>Easy Money Platform - Trusted PayPal Income</h1><p>Find the best ways to make money online with trusted platforms that pay through PayPal.</p><script src='script.js'></script></body></html>\"", "WRITE_FILE easy_money_platform/style.css \"body { font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; } h1 { color: #333; }\"", "WRITE_FILE easy_money_platform/script.js \"console.log('Easy Money Platform loaded');\""]
                    else:
                        rule_based_plan = [f"CREATE_PROJECT {project_name}"]
                elif "make website for easy money" in sp or "easy money platform" in sp or "open a trusted platform" in sp:
                    rule_based_plan = ["CREATE_PROJECT easy_money_platform", "WRITE_FILE easy_money_platform/index.html \"<!DOCTYPE html><html><head><title>Easy Money Platform</title><link rel='stylesheet' href='style.css'></head><body><h1>Easy Money Platform - Trusted PayPal Income</h1><p>Find the best ways to make money online with trusted platforms that pay through PayPal.</p><ul><li>Freelancing</li><li>Online Surveys</li><li>Affiliate Marketing</li><li>Content Creation</li></ul><script src='script.js'></script></body></html>\"", "WRITE_FILE easy_money_platform/style.css \"body { font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; } h1 { color: #333; }\"", "WRITE_FILE easy_money_platform/script.js \"console.log('Easy Money Platform loaded');\""]
                elif any(x in sp for x in ["find a website", "find a site", "find website", "find site", "look for a website", "look for website", "search for a website"]):
                    rule_based_plan = ["HOTKEY ctrl l", "TYPE google.com", "PRESS enter", "WAIT 2.0"]
                elif any(x in sp for x in ["learn python", "learning python", "python tutorial", "python course", "open a learning python site"]):
                    python_sites = {
                        "python.org": "python.org",
                        "real python": "realpython.com",
                        "w3schools": "w3schools.com/python",
                        "codecademy": "codecademy.com/learn/learn-python-3",
                        "coursera": "coursera.org/specializations/python",
                        "udemy": "udemy.com/course/complete-python-bootcamp/",
                    }
                    target_site = "python.org"
                    for keyword, site in python_sites.items():
                        if keyword in sp:
                            target_site = site
                            break
                    rule_based_plan = ["HOTKEY ctrl l", f"TYPE {target_site}", "PRESS enter", "WAIT 2.0"]
                
            if rule_based_plan:
                mini_plan = rule_based_plan
                print(f"[🔧 RULE] Matched pattern for: '{sub_prompt}' -> {rule_based_plan}")
                print(f"[   Plan] {json.dumps(mini_plan)} [RULE-BASED]")
                reason_overlay(f"[PLAN {task_idx+1}]\n{json.dumps(mini_plan)}\n[RULE-BASED]")
            else:
                mini_prompt = f"""Current goal: {sub_prompt}
Current screen: {context.get('screen_summary', 'unknown')}
WAITING: {'DISABLED - skip all WAIT commands' if not waiting_enabled else 'Enabled - use WAIT for loading'}
{youtube_rule}

CRITICAL RULES:
- NEVER include the word "search" inside a TYPE argument. Use "TYPE minecraft", NOT "TYPE search minecraft".
- If the goal involves searching and then visiting/clicking a result, include "click first search result" as the final step.
- For YouTube video clicks: ALWAYS use "CLICK video". This uses thumbnail-based detection to find the actual video thumbnail. Do NOT use "FIND_TEXT" for videos.
- For vague goals like "find a website" or "open a website": use "HOTKEY ctrl l", "TYPE google.com", "PRESS enter", "WAIT 2.0".
- For "open a learning python site" or "learn python": navigate directly to python.org or realpython.com. Use "HOTKEY ctrl l", "TYPE python.org", "PRESS enter", "WAIT 2.0".
- NEVER execute vague pronoun queries like "find it", "search it", "look for it" literally. If the query is just a pronoun without context, return CHAT_RESPONSE asking what to search for.
- NEVER output chat or ask for confirmation. Output ONLY JSON like: ["STEP1", "STEP2"]"""

                response = ollama_chat(
                    model=OLLAMA_MODEL,
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": mini_prompt}
                    ],
                    temperature=0.0,
                    max_tokens=256
                )
                
                import re as _re2
                raw_response = response['choices'][0]['message']['content']
                json_match = _re2.search(r'\[.*?\]', raw_response, _re2.DOTALL)
                if not json_match:
                    print(f"[⚠️] No plan generated for: {sub_prompt}")
                    # Fallback for vague search/website queries
                    if any(x in sp for x in ["find a website", "find website", "search for a website", "open a website", "find a site"]):
                        print(f"[🔧 FALLBACK] Using default browser navigation for vague query: {sub_prompt}")
                        mini_plan = ["HOTKEY ctrl l", "TYPE google.com", "PRESS enter", "WAIT 2.0"]
                    else:
                        continue
                
                try:
                    mini_plan = json.loads(json_match.group())
                except json.JSONDecodeError:
                    print(f"[⚠️] Invalid plan for: {sub_prompt}")
                    continue
                
                mini_plan = _merge_split_commands(mini_plan)
                mini_plan = [s for s in mini_plan if _is_valid_command(s)]
                if not mini_plan:
                    print(f"[⚠️] No valid commands generated for: {sub_prompt}")
                    # Fallback for vague search/website queries
                    if any(x in sp for x in ["find a website", "find website", "search for a website", "open a website", "find a site", "find site", "look for a website", "look for website"]):
                        print(f"[🔧 FALLBACK] Using default browser navigation for vague query: {sub_prompt}")
                        mini_plan = ["HOTKEY ctrl l", "TYPE google.com", "PRESS enter", "WAIT 2.0"]
                    else:
                        continue
                
                print(f"[   Plan] {json.dumps(mini_plan)}")
                reason_overlay(f"[PLAN {task_idx+1}]\n{json.dumps(mini_plan)}")
            
                # Execute the mini-plan
                for step_idx, step in enumerate(mini_plan):
                    if not isinstance(step, str):
                        print(f"[⚠️ STEP] Skipping non-string step: {step!r}")
                        continue
                    print(f"\n--- [Step {step_idx+1}/{len(mini_plan)}] ---")
                print(f"[📌 CURRENT TASK] {step}")
                
                detect_screen_context(context)
                if context.get("screen_summary"):
                    print(f"[🖥️ SCREEN] {context['screen_summary'][:200]}")
                
                reason_overlay(f"[DOING] {step}\n[Sub-task {task_idx+1}]\n[Step {step_idx+1}/{len(mini_plan)}]")
                set_status_text(f"SUBTASK {task_idx+1}/{len(sub_tasks)}: {sub_prompt}\nSTEP: {step}")
                
                success, context = execute_macro_step(step, context)
                
                if success:
                    next_step = mini_plan[step_idx+1] if step_idx+1 < len(mini_plan) else "DONE"
                    print(f"[✅ DONE] {step} -> Next: {next_step}")
                    reason_overlay(f"[DONE] {step}\n[NEXT] {next_step}")
                else:
                    print(f"[❌ FAILED] {step}: {context.get('last_error', 'unknown')[:60]}" )
                    reason_overlay(f"[FAILED] {step}\n[ERROR] {context.get('last_error', 'unknown')[:80]}")
                    
                    # ===== RETRY WITH ALTERNATIVE STRATEGIES =====
                    print(f"[🔁 RETRY] Step failed, attempting alternative strategies...")
                    retry_success, context = retry_failed_action(step, context)
                    if retry_success:
                        print(f"[✅ RETRY SUCCESS] Alternative strategy worked for: {step}")
                        success = True
                        reason_overlay(f"[DONE] {step} (via retry)")
                
                if not success and context.get("last_error"):
                    print(f"[⚠️ STEP ERROR] {context['last_error']}")
                
                # Reset retry counter on each step attempt
                context["_retry_attempt"] = 0
                
                # Auto-inject SELECT_PROFILE after LAUNCH chrome succeeds
                if step.lower().strip() == "launch chrome" and success:
                    print(f"[👤 AUTO] Injecting SELECT_PROFILE after Chrome launch")
                    reason_overlay(f"[AUTO] Chrome launched - selecting profile...")
                    success_profile, context = execute_macro_step("SELECT_PROFILE", context)
                    if not success_profile:
                        print(f"[⚠️ AUTO] Profile selection failed, continuing...")
                
                # Brief pause between steps (skip if WAITING disabled)
                if waiting_enabled:
                    time.sleep(0.3)
        
        print(f"\n[🎉 COMPLETE] All {len(sub_tasks)} sub-tasks executed successfully!")
        reason_overlay(f"[🎉 ALL DONE]\nCompleted {len(sub_tasks)} sub-tasks\nTask: {user_macro_goal[:50]}")
        _add_conversation("AI", f"All {len(sub_tasks)} sub-tasks completed successfully.", meta={
            "window": initial_context.get("screen_active_window", ""),
            "screen_summary": initial_context.get("screen_summary", ""),
            "steps": sub_tasks,
            "type": "task_complete"
        })
        
    except ChatReply as cr:
        reply_text = cr.reply_text
        print(f"\n[💬 AI] {reply_text}")
        reason_overlay(f"[💬 AI]\n{reply_text}")
        embedded_actions = _extract_actions_from_reply(reply_text)
        if embedded_actions:
            print(f"[🔧 EMBEDDED ACTIONS] Executing: {embedded_actions}")
            exec_ctx = {"workspace": os.getcwd(), "retry_count": 0, "skip_waits": False, "_retry_attempt": 0}
            for action in embedded_actions:
                success, exec_ctx = execute_macro_step(action, exec_ctx)
                if not success:
                    print(f"[⚠️ EMBEDDED ACTION FAILED] {exec_ctx.get('last_error', 'unknown')}")
                    break
    except Exception as e:
        print(f"[❌ ERROR] {e}")
        import traceback
        traceback.print_exc()
        _add_conversation("System", f"Error: {str(e)[:120]}", meta={
            "window": initial_context.get("screen_active_window", ""),
            "error": str(e)[:120],
            "type": "error"
        })
    
    _paused = False
    _stop_requested = False

_paused = False
_stop_requested = False

def show_quick_prompt():
    """Show a small overlay prompt window for quick task input."""
    import tkinter as tk
    
    root = tk.Tk()
    root.title("MaxAI Quick Prompt")
    root.geometry("500x260")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#1e1e2e")
    
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    root.geometry(f"500x260+10+{screen_h-280}")
    
    ctx = {"workspace": os.getcwd()}
    detect_screen_context(ctx)
    win_name = ctx.get("screen_active_window", "unknown")
    screen_summary = ctx.get("screen_summary", "")
    context_note = _get_screen_context_explanation(
        win_name,
        ctx.get("screen_is_ide", False),
        ctx.get("screen_is_browser", False),
        ctx.get("screen_ide_type", "")
    )
    
    label = tk.Label(root, text="🚀 Enter automation task:", 
                     bg="#1e1e2e", fg="#a6e3a8", font=("Segoe UI", 12, "bold"))
    label.pack(pady=(20, 5))
    
    ctx_label = tk.Label(root, text=f"Current: {context_note}", 
                         bg="#1e1e2e", fg="#c6d0f5", font=("Segoe UI", 9),
                         wraplength=480, justify="left")
    ctx_label.pack(pady=(0, 10))
    
    entry = tk.Entry(root, font=("Segoe UI", 11), width=50, 
                     bg="#303446", fg="#c6d0f5", insertbackground="#a6e3a8")
    entry.pack(pady=10)
    entry.focus()
    
    btn_frame = tk.Frame(root, bg="#1e1e2e")
    btn_frame.pack(pady=10)
    
    submit_btn = tk.Button(btn_frame, text="Execute", 
                           bg="#89b4fa", fg="#1e1e2e", 
                           font=("Segoe UI", 10, "bold"),
                           command=lambda: on_submit(), relief="flat")
    submit_btn.pack(side="left", padx=10)
    
    def on_submit(event=None):
        global _stop_requested
        prompt = entry.get()
        if prompt.strip():
            root.destroy()
            _stop_requested = False
            run_quick_task(prompt)
        else:
            root.destroy()
            _stop_requested = False
    
    def on_escape(event):
        global _stop_requested
        _stop_requested = True
        root.destroy()
    
    root.bind("<Return>", lambda e: on_submit())
    root.bind("<Escape>", on_escape)
    root.mainloop()

def show_pause_prompt():
    """Show pause prompt in top-left corner."""
    import tkinter as tk
    
    root = tk.Tk()
    root.title("MaxAI Paused")
    root.geometry("300x100")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#1e1e2e")
    
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    root.geometry(f"300x100+10+10")
    
    label = tk.Label(root, text="⏸ Paused - Press Ctrl+P to resume", 
                     bg="#1e1e2e", fg="#f9e2af", font=("Segoe UI", 10))
    label.pack(pady=15)
    
    root.mainloop()

def detect_error_popup():
    """Check for error dialogs or popups that appeared."""
    import uiautomation as auto
    try:
        active_window = auto.GetForegroundControl()
        if not active_window:
            return None
        window_name = active_window.Name.lower() if active_window.Name else ""
        
        error_indicators = ["error", "exception", "failed", "warning", "alert", 
                          "could not", "unable", "not found", "access denied"]
        
        if any(ind in window_name for ind in error_indicators):
            return window_name
        
        for control, depth in auto.WalkControl(active_window, maxDepth=3):
            ctrl_name = control.Name.lower() if control.Name else ""
            if any(ind in ctrl_name for ind in error_indicators):
                if control.ControlTypeName in ["WindowTextControl", "TextControl"]:
                    return f"error_text: {ctrl_name}"
    except:
        pass
    return None

def create_project_structure(project_path, project_name, files_dict):
    """Create full project with folders and multiple files.
    files_dict: {"folder/file.py": "content", "another.py": "more content"}
    """
    full_path = os.path.abspath(project_path)
    os.makedirs(full_path, exist_ok=True)
    
    created_files = []
    for file_path, content in files_dict.items():
        file_full = os.path.join(full_path, file_path)
        os.makedirs(os.path.dirname(file_full), exist_ok=True)
        with open(file_full, 'w', encoding='utf-8') as f:
            f.write(content)
        created_files.append(file_full)
        print(f"[📝 PROJECT] Created: {file_full}")
    
    return full_path, created_files

def run_in_ide(file_path, ide_type="vscode"):
    """Run a script in VS Code or Antigravity IDE."""
    if ide_type == "vscode":
        execute_macro_step("VSCODE_ACTION open_file", {"workspace": os.getcwd()})
        time.sleep(1)
        execute_macro_step(f"TYPE_IN open \"{file_path}\"", {"workspace": os.getcwd()})
        time.sleep(0.5)
        pyautogui.press('enter')
    elif ide_type == "antigravity":
        execute_macro_step("ANTIGRAVITY_ACTION open_file", {"workspace": os.getcwd()})
        time.sleep(1)
        execute_macro_step(f"TYPE_IN open \"{file_path}\"", {"workspace": os.getcwd()})
        time.sleep(0.5)
        pyautogui.press('enter')

def run_quick_task(prompt):
    """Run a quick automation task without full terminal UI."""
    import threading
    context = {"workspace": os.getcwd(), "retry_count": 0, "_retry_attempt": 0}
    
    def execute():
        import subprocess
        pyautogui.FAILSAFE = True
        task_prompt = prompt
        context = {"workspace": os.getcwd(), "retry_count": 0, "_retry_attempt": 0}
        print(f"\n[⚡ QUICK PROMPT] {task_prompt}")
        
        # Parse WAITING flag from prompt
        task_prompt_lower = task_prompt.lower().strip()
        if task_prompt_lower.startswith("waiting false") or task_prompt_lower.startswith("waiting off"):
            set_skip_waits(True)
            task_prompt = " ".join(task_prompt.split(" ", 2)[2:]) if len(task_prompt.split(" ", 2)) > 2 else task_prompt
            print("[⚡ WAITING] FALSE detected in prompt - all WAIT commands will be skipped")
        elif task_prompt_lower.startswith("waiting true") or task_prompt_lower.startswith("waiting on"):
            set_skip_waits(False)
            task_prompt = " ".join(task_prompt.split(" ", 2)[2:]) if len(task_prompt.split(" ", 2)) > 2 else task_prompt
            print("[⚡ WAITING] TRUE detected in prompt - WAIT commands enabled")
        
        confirmation_words = ["yes", "yeah", "yep", "sure", "ok", "okay", "proceed", "go ahead", "do it", "please do", "please proceed", "confirm", "confirmed", "affirmative", "yup", "ya", "yeh"]
        if task_prompt_lower.strip().strip(".!?,") in confirmation_words or any(task_prompt_lower.startswith(w) for w in confirmation_words):
            pending_task = _get_and_clear_pending_verification()
            if pending_task:
                print(f"[✅ CONFIRM] User confirmed pending task: {pending_task}")
                task_prompt = pending_task
                task_prompt_lower = task_prompt.lower().strip()
        
        system_instruction = (
            "You are a hyper-precise OS automation orchestrator with full system visibility. Your name is MaxAI\n"
            "Your goal is 100% reliable task execution, specialized in robust project management and multi-file coding workflows.\n\n"
            "SYSTEM UNDERSTANDING:\n"
            "- The system can see all open windows, their titles, and contents via UI Automation (uiautomation)\n"
             "- You have access to keyboard shortcuts, direct file operations, and visual detection\n\n"
            "CRITICAL RULES FOR 100% ACCURACY:\n"
             "1. YOUTUBE SEARCH OVERRIDE (HIGHEST PRIORITY): When on YouTube, ALWAYS use 'PRESS /' to focus the YouTube search bar. THEN TYPE the query. THEN PRESS enter. NEVER use 'HOTKEY ctrl l', 'BROWSER_ACTION focus_address', or any address-bar shortcut on YouTube. '/' is YouTube's own search shortcut.\n"
            "2. SCREEN ANALYSIS BEFORE ACTION: Run SCREEN_ANALYZE at the start of EVERY task to understand context. Use the visible text and window title to guide your plan.\n"
            "3. CONTEXTUAL CONTEXT CHECK: BEFORE ANY ACTION, check if screen_is_browser is True in screen_summary. If TRUE, you are in a browser - determine if it's YouTube or another site. If FALSE, work in the active application (IDE, chat app, etc.).\n"
             "4. BROWSER CONTEXT (NON-YOUTUBE): If the active window contains 'chrome' or 'edge' but NOT 'youtube', use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address' to focus the address bar BEFORE searching.\n"
            "5. DISCORD/CHAT APPS: For Discord, Slack, Teams, Telegram, or any chat app: use FIND_TEXT to read messages, SCROLL to navigate history, CHAT_REPLY to generate and send a contextual reply (preferred), or TYPE to reply manually. NEVER open a new window for chat tasks.\n"
            "6. CHAT_REPLY: For conversational tasks in chat apps, use CHAT_REPLY. It analyzes visible messages and generates a contextual reply automatically. Use LOOP for multiple replies.\n"
            "7. SCROLL: Use SCROLL <amount> to scroll the active window (positive=down, negative=up). Critical for chat apps and feeds.\n"
            "8. KEYBOARD SHORTCUTS > CLICKING ALWAYS. Never try to visually locate UI elements when a keyboard shortcut exists.\n"
             "9. For general browser address bar (NOT YouTube): ALWAYS use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address', NEVER look for 'address_bar' visually.\n"
            "10. CHROME PROFILE: After LAUNCH chrome (or when seeing 'Profile Picker' window), you MUST add 'SELECT_PROFILE' to handle the profile picker screen.\n"
            "11. WINDOW FOCUS: If an action fails because a window is hidden, use 'FOCUS_APP <app_name>' first (e.g., 'FOCUS_APP code', 'FOCUS_APP antigravity').\n"
            "12. If a step fails, the system will automatically retry with corrected steps using SELF_CORRECT.\n"
            "13. Only use visual detection (CLICK/DOUBLE_CLICK) when no keyboard shortcut exists.\n"
            "14. WAIT COMMANDS: Use WAIT after actions that need time to load. BUT if the task prompt contains 'WAITING FALSE', DO NOT include any WAIT commands - execute steps back-to-back.\n"
             "15. OPENING PROGRAMS (EXCEPT CHROME): For VS Code, Antigravity, and all other applications except Chrome, ALWAYS open via Windows search: 'PRESS win', 'WAIT 1.0', 'TYPE \"<app_name>\"', 'WAIT 1.5', 'PRESS enter'. FOR CHROME: use ONLY 'LAUNCH chrome' - it handles launching AND focusing automatically. NEVER use 'PRESS win' before 'LAUNCH chrome'.\n"
             "16. CLICK STRATEGY: Use descriptive semantic labels matching visible text. NEVER use numbers or generic labels. Match against visible text elements.\n"
             "17. EXPECTATIONS: Use EXPECT to verify outcomes: 'EXPECT <element> text_contains_any \"YouTube,Video\"' or 'EXPECT <element> window_title \"YouTube\"'.\n"
             "18. FILE CREATION: For desktop file creation, ALWAYS use CREATE_FILE directly. Never navigate a file picker dialog.\n"
              "19. VIDEO THUMBNAILS: To click videos on YouTube, ALWAYS use 'CLICK video'. This triggers thumbnail-based detection using Florence-2 vision model to find the actual video thumbnail. Do NOT use 'FIND_TEXT' for videos.\n"
             "20. FULL SCREEN: FOR YOUTUBE VIDEO USE PRESS F, AND FOR OTHER WEBSITES USE PRESS F11.\n"
             "21. CRITICAL: If screen_active_window is 'Program Manager', 'Desktop', or similar, NEVER use BROWSER_ACTION, HOTKEY ctrl l, or any browser shortcut. Instead use LAUNCH chrome to open the browser FIRST, then use BROWSER_ACTION.\n\n"
             "22. CONTEXTUAL DECISION MAKING: If screen_summary shows 'Browser window active' and 'YouTube detected', use 'PRESS /' to search. If browser but NOT YouTube, use BROWSER_ACTION focus_address. If screen_summary shows 'IDE detected', work within the IDE. If neither, analyze visible text to determine the correct action.\n\n"
             "23. CURRENT WINDOW FIRST: If the user's task is about what's currently on screen (chat, forms, editors, browsers), ALWAYS work in the ACTIVE WINDOW. Do NOT launch new apps unless explicitly asked.\n"
            "Available commands:\n"
            "- LAUNCH <app_name>  (ONLY for chrome; for others use Windows search)\n"
            "- HOTKEY <key1> <key2> ...  (preferred over CLICK - e.g., 'HOTKEY ctrl l')\n"
            "- PRESS <key_name>  (single key press)\n"
            "- TYPE \"<text>\"  (type at current cursor)\n"
            "- TYPE_IN <element> \"<text>\"  (click then type - only when no shortcut)\n"
            "- CLICK <target_element>  (last resort - use visible text label)\n"
            "- DOUBLE_CLICK <target_element>\n"
            "- RIGHT_CLICK <target_element>\n"
            "- MOVE_TO <x> <y> [duration]\n"
            "- MOVE_REL <dx> <dy> [duration]\n"
            "- SCROLL <amount>  (positive=down, negative=up)\n"
            "- FIND_TEXT <text>  (find via OCR, move mouse there)\n"
            "- FIND_IMAGE <name>  (find via template matching)\n"
            "- SCREEN_ANALYZE  (scan screen: active window, visible text, regions - run BEFORE FOCUS_APP or CLICK)\n"
            "- CHAT_REPLY  (analyze chat and generate contextual reply)\n"
            "- WAIT <seconds>\n"
            "- CREATE_FILE <file_path>\n"
            "- WRITE_FILE <file_path> \"<content>\"\n"
            "- READ_FILE <file_path>\n"
            "- DELETE_FILE <file_path>\n"
            "- RUN_FILE <file_path>\n"
            "- LIST_FILES <directory_path>\n"
            "- SET_WORKSPACE <directory_path>\n"
            "- FOCUS_APP <app_name>  (focus window by name)\n"
            "- SELECT_PROFILE  (MUST use after LAUNCH chrome or when seeing Profile Picker)\n"
            "- CHAT_REPLY  (generate and send chat reply)\n"
            "- BROWSER_ACTION <action_name>  (focus_address, new_tab, close_tab, refresh, back, forward, home)\n"
            "- SELF_CORRECT  (auto-retries on failure)\n"
            "- WAITING <true/false>  (set to FALSE to skip all WAIT commands)\n\n"
            "EXAMPLES:\n"
            "YouTube search (focused plan):\n"
            "[\"PRESS /\", \"TYPE \\\"horror videos\\\"\", \"PRESS enter\"]\n\n"
            "Open browser and search (focused plan):\n"
            "[\"PRESS win\", \"TYPE \\\"chrome\\\"\", \"PRESS enter\", \"WAIT 3.0\", \"HOTKEY ctrl l\"]\n\n"
            "Create file on Desktop (direct):\n"
            "[\"SCREEN_ANALYZE\", \"CREATE_FILE \\Desktop\\notes.txt\", \"WRITE_FILE \\Desktop\\notes.txt \\\"My notes here\\\"\"]\n\n"
            "If you see 'Profile Picker' or 'Who's Using Chrome' window title, use SELECT_PROFILE.\n"
            "If element can't be found: run SCREEN_ANALYZE, try alternative labels, then SELF_CORRECT.\n\n"
            "Respond with ONLY a valid JSON array. NO PROSE, NO MARKDOWN, NO EXPLANATION.")
        
        try:
            # Step 0: Analyze screen FIRST so decomposition knows the current state
            detect_screen_context(context)
            initial_context = context.get('screen_active_window', 'unknown')
            is_youtube = context.get('screen_is_youtube', False)
            _add_conversation("You", task_prompt, meta={
                "window": context.get("screen_active_window", ""),
                "screen_summary": context.get("screen_summary", ""),
                "is_browser": context.get("screen_is_browser", False),
                "is_ide": context.get("screen_is_ide", False)
            })
            
            # Step 1: Decompose complex tasks into smaller focused sub-tasks WITH screen context
            sub_tasks = decompose_task(task_prompt, context)
            print(f"[📋 TASK BREAKDOWN] {len(sub_tasks)} sub-tasks: {sub_tasks}")
            
            reason_overlay(f"[START] {prompt}\n[STEPS] {len(sub_tasks)} sub-tasks\n[WINDOW] {initial_context}\n[YOUTUBE] {'YES' if is_youtube else 'NO'}\n[WAITING] {'OFF' if get_skip_waits() else 'ON'}")
            
            previous_results = []
            
            # Step 2: Execute each sub-task with its own focused mini-plan
            for task_idx, sub_prompt in enumerate(sub_tasks):
                global _stop_requested
                if _stop_requested:
                    print("[🛑 STOP] User requested stop via ESC")
                    break
                
                # ===== REASONING DISPLAY =====
                border = "=" * 60
                print(f"\n{border}")
                print(f"[🎯 SUB-TASK {task_idx+1}/{len(sub_tasks)}] {sub_prompt}")
                print(f"[📌 FOCUS] Currently working on: {sub_prompt}")
                print(f"[🖥️ WINDOW] {context.get('screen_active_window', 'checking...')}")
                print(f"[🌐 YOUTUBE] {'YES - use / for search' if context.get('screen_is_youtube') else 'NO'}")
                print(f"[🌍 BROWSER] {'YES' if context.get('screen_is_browser') else 'NO'}")
                print(f"[⚙️ IDE] {'YES - ' + context.get('screen_ide_type', '') if context.get('screen_is_ide') else 'NO'}")
                print(f"[⏱️ WAITING] {'OFF (skip all)' if get_skip_waits() else 'ON (normal)'}")
                print(f"{border}")
                
                reason_overlay(f"[FOCUS {task_idx+1}/{len(sub_tasks)}]\n{sub_prompt}\n\n"
                              f"Window: {context.get('screen_active_window', 'checking...')}\n"
                              f"YouTube: {'YES' if context.get('screen_is_youtube') else 'NO'}\n"
                              f"Browser: {'YES' if context.get('screen_is_browser') else 'NO'}\n"
                              f"Waiting: {'OFF' if get_skip_waits() else 'ON'}")
                
                # Detect context for THIS specific sub-task
                detect_screen_context(context)
                is_youtube = context.get('screen_is_youtube', False)
                is_browser = context.get('screen_is_browser', False)
                
                # Generate a SHORT, FOCUSED mini-plan (1-3 steps) for just this sub-task
                waiting_note = "NOTE: WAIT commands are DISABLED - skip all waits." if get_skip_waits() else "NOTE: Use WAIT commands for loading times."
                
                previous_context = ""
                if previous_results:
                    previous_context = f"\nPrevious steps completed: {'; '.join(previous_results[-3:])}"
                
                youtube_rule = ""
                if is_youtube:
                    youtube_rule = "\nCRITICAL YOUTUBE RULE: You are on YouTube. To search, use 'PRESS /' then TYPE then PRESS enter. NEVER use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address' on YouTube."
                elif is_browser:
                    youtube_rule = "\nBROWSER RULE: You are in a browser (NOT YouTube). To focus address bar, use 'HOTKEY ctrl l' or 'BROWSER_ACTION focus_address'."
                
                # Add specific instructions based on sub-task type
                task_specific_rules = ""
                if "video" in sub_prompt.lower() or "click" in sub_prompt.lower():
                    task_specific_rules = "\nVIDEO CLICKING RULE: To click a YouTube video, ALWAYS use 'CLICK video'. This triggers thumbnail-based detection which finds the actual video thumbnail on screen. Do NOT use 'FIND_TEXT' for videos - it will click on text labels instead of the video itself."
                elif "search" in sub_prompt.lower():
                    task_specific_rules = "\nSEARCH RULE: Use the appropriate search method based on context (YouTube: PRESS /, Browser: HOTKEY ctrl l)."
                
# ===== RULE-BASED MINI-PLAN (bypass AI when vision is dead or context is clear) =====
                sp = sub_prompt.lower().strip()
                rule_based_plan = None
                
                if sp == "launch chrome":
                    rule_based_plan = ["LAUNCH chrome", "SELECT_PROFILE"]
                elif sp.startswith("launch ") or sp.startswith("open "):
                    app_name = sp.replace("launch ", "").replace("open ", "").strip()
                    app_name = app_name.title()
                    rule_based_plan = ["PRESS win", f"TYPE {app_name}", "WAIT 1.5", "PRESS enter", "WAIT 2.0"]
                    if "epic" in sp:
                        rule_based_plan = ["PRESS win", "TYPE Epic Games", "WAIT 1.5", "PRESS enter", "WAIT 3.0"]
                elif sp in ["go to youtube.com", "open youtube", "go to youtube"]:
                    rule_based_plan = ["HOTKEY ctrl l", "TYPE youtube.com", "PRESS enter", "WAIT 3.0"]
                elif "search for" in sp and "youtube" in sp:
                    query = sp.replace("search for", "").replace("youtube", "").strip()
                    query = query.replace(" and click it", "").replace(" on ", " ").replace(" and put ", " ").strip()
                    query = query.replace("open yotube", "").replace("go to yotube", "").strip()
                    # Fix common typos in channel names
                    typo_fixes = {"markpleir": "markiplier", "markplier": "markiplier", "peewdiepie": "pewdiepie"}
                    for typo, correct in typo_fixes.items():
                        query = query.replace(typo, correct)
                    for prefix in ["open ", "go ", "a ", "the ", "good "]:
                        query = query.strip()
                        if query.startswith(prefix):
                            query = query[len(prefix):].strip()
                    if not query:
                        query = "pewdiepie"
                    rule_based_plan = ["PRESS /", f"TYPE {query}", "PRESS enter", "WAIT 2.0"]
                elif "click" in sp and "video" in sp:
                    # Always use thumbnail-based video clicking for YouTube
                    rule_based_plan = ["CLICK video"]
                elif any(x in sp for x in ["find a website", "find a site", "find website", "find site", "look for a website", "look for website", "search for a website", "open a website"]):
                    rule_based_plan = ["HOTKEY ctrl l", "TYPE google.com", "PRESS enter", "WAIT 2.0"]
                elif any(x in sp for x in ["learn python", "learning python", "python tutorial", "python course", "open a learning python site"]):
                    python_sites = {
                        "python.org": "python.org",
                        "real python": "realpython.com",
                        "w3schools": "w3schools.com/python",
                        "codecademy": "codecademy.com/learn/learn-python-3",
                        "coursera": "coursera.org/specializations/python",
                        "udemy": "udemy.com/course/complete-python-bootcamp/",
                    }
                    target_site = "python.org"
                    for keyword, site in python_sites.items():
                        if keyword in sp:
                            target_site = site
                            break
                    rule_based_plan = ["HOTKEY ctrl l", f"TYPE {target_site}", "PRESS enter", "WAIT 2.0"]
                elif sp == "press f" or sp == "make it fullscreen" or sp == "make it full screen" or "fullscreen" in sp:
                    rule_based_plan = ["PRESS f"]
                elif sp == "focus address bar" or sp == "focus the address bar":
                    rule_based_plan = ["HOTKEY ctrl l"]
                elif "type" in sp and "youtube" in sp:
                    rule_based_plan = ["PRESS /", f"TYPE {sp.replace('type', '').replace('on youtube', '').replace('youtube', '').strip()}", "PRESS enter"]
                
                # Antigravity rules
                if not rule_based_plan:
                    antigravity_already_open = context.get("screen_is_ide") and context.get("screen_ide_type") == "antigravity"
                    
                    if sp == "launch antigravity":
                        if antigravity_already_open:
                            rule_based_plan = ["WAIT 0.5"]
                        else:
                            rule_based_plan = ["PRESS win", "TYPE Antigravity", "WAIT 1.5", "PRESS enter", "WAIT 2.0"]
                    elif any(x in sp for x in ["open and make website", "open the folder", "start building website", "open folder for website"]):
                        rule_based_plan = ["WAIT 0.5"]
                    elif ("create new project" in sp or "create website project" in sp or "make website project" in sp) and "antigravity" in sp:
                        project_name = sp.replace("create new project in antigravity", "").replace("create project in antigravity", "").replace("create website project in antigravity", "").replace("website project in antigravity", "").replace("make website project in antigravity", "").strip()
                        if not project_name:
                            project_name = "my_project"
                        rule_based_plan = [f"CREATE_PROJECT {project_name}"]
                    elif "make website project" in sp and "antigravity" in sp:
                        project_name = sp.replace("make website project in antigravity", "").replace("website project in antigravity", "").strip()
                        if not project_name:
                            project_name = "my_website"
                        rule_based_plan = [f"CREATE_PROJECT {project_name}", f"WRITE_FILE {project_name}/index.html \"<!DOCTYPE html><html><head><title>{project_name}</title><link rel='stylesheet' href='style.css'></head><body><h1>Welcome to {project_name}</h1><script src='script.js'></script></body></html>\"", f"WRITE_FILE {project_name}/style.css \"body {{ font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; }} h1 {{ color: #333; }}\"", f"WRITE_FILE {project_name}/script.js \"console.log('{project_name} loaded');\""]
                    elif "website" in sp and "antigravity" in sp:
                        rule_based_plan = ["CREATE_PROJECT my_website", "WRITE_FILE my_website/index.html \"<!DOCTYPE html><html><head><title>My Website</title><link rel='stylesheet' href='style.css'></head><body><h1>Welcome</h1><script src='script.js'></script></body></html>\"", "WRITE_FILE my_website/style.css \"body { font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; }\"", "WRITE_FILE my_website/script.js \"console.log('Website loaded');\""]
                
                if rule_based_plan:
                    mini_plan = rule_based_plan
                    print(f"[   Plan] {json.dumps(mini_plan)} [RULE-BASED]")
                    reason_overlay(f"[PLAN {task_idx+1}]\n{json.dumps(mini_plan)}\n[RULE-BASED]")
                else:
                    mini_prompt = f"""Current goal: {sub_prompt}
Current screen: {context.get('screen_summary', 'unknown')}
WAITING status: {'SKIP ALL WAITS (0s delays)' if get_skip_waits() else 'Normal waits enabled'}
{youtube_rule}{task_specific_rules}{previous_context}

CRITICAL RULES:
- NEVER include the word "search" inside a TYPE argument. Use "TYPE minecraft", NOT "TYPE search minecraft".
- If the goal involves searching and then visiting/clicking a result, include "click first search result" as the final step.
- For YouTube video clicks: ALWAYS use "CLICK video". This uses thumbnail-based detection to find the actual video thumbnail. Do NOT use "FIND_TEXT" for videos.
- For vague goals like "find a website" or "open a website": use "HOTKEY ctrl l", "TYPE google.com", "PRESS enter", "WAIT 2.0".
- For "open a learning python site" or "learn python": navigate directly to python.org or realpython.com. Use "HOTKEY ctrl l", "TYPE python.org", "PRESS enter", "WAIT 2.0".
- NEVER execute vague pronoun queries like "find it", "search it", "look for it" literally. If the query is just a pronoun without context, return CHAT_RESPONSE asking what to search for.
- NEVER output chat or ask for confirmation. Output ONLY JSON like: ["STEP1", "STEP2"]"""

                    response = ollama_chat(
                        model=OLLAMA_MODEL,
                        messages=[
                            {"role": "system", "content": system_instruction},
                            {"role": "user", "content": mini_prompt}
                        ],
                        temperature=0.0,
                        max_tokens=256
                    )
                    
                    import re as _re2
                    raw_response = response['choices'][0]['message']['content']
                    json_match = _re2.search(r'\[.*?\]', raw_response, _re2.DOTALL)
                    if not json_match:
                        print(f"[⚠️] No plan generated for: {sub_prompt}")
                        continue
                    
                    try:
                        mini_plan = json.loads(json_match.group())
                    except json.JSONDecodeError:
                        print(f"[⚠️] Invalid plan for: {sub_prompt}")
                        continue
                    
                    mini_plan = _merge_split_commands(mini_plan)
                    mini_plan = [s for s in mini_plan if _is_valid_command(s)]
                    if not mini_plan:
                        print(f"[⚠️] No valid commands generated for: {sub_prompt}")
                        # Fallback for vague search/website queries
                        if any(x in sp for x in ["find a website", "find website", "search for a website", "open a website", "find a site", "find site", "look for a website", "look for website"]):
                            print(f"[🔧 FALLBACK] Using default browser navigation for vague query: {sub_prompt}")
                            mini_plan = ["HOTKEY ctrl l", "TYPE google.com", "PRESS enter", "WAIT 2.0"]
                        else:
                            continue
                    
                    print(f"[   Plan] {json.dumps(mini_plan)}")
                
                    # Execute the mini-plan steps
                    for step_idx, step in enumerate(mini_plan):
                        if _stop_requested:
                            print("[🛑 STOP] User requested stop via ESC")
                            break
                        if not isinstance(step, str):
                            print(f"[⚠️ STEP] Skipping non-string step: {step!r}")
                            continue
                        
                        print(f"[   Step {step_idx+1}/{len(mini_plan)}] {step}")
                        reason_overlay(f"[DOING {task_idx+1}/{len(sub_tasks)}] {sub_prompt}\n[STEP {step_idx+1}/{len(mini_plan)}] {step}")
                        set_status_text(f"SUBTASK {task_idx+1}/{len(sub_tasks)}: {sub_prompt}\nSTEP: {step}")
                        
                        success, context = execute_macro_step(step, context)
                    
                    if success:
                        next_step = mini_plan[step_idx+1] if step_idx+1 < len(mini_plan) else "DONE"
                        if next_step == "DONE":
                            next_sub = sub_tasks[task_idx+1] if task_idx+1 < len(sub_tasks) else "ALL COMPLETE"
                            reason_overlay(f"[DONE] {step}\n[NEXT] {next_sub}")
                        else:
                            reason_overlay(f"[DONE] {step}\n[NEXT] {next_step}")
                        print(f"[   DONE] {step}")
                    else:
                        reason_overlay(f"[FAILED] {step}\n[ERROR] {context.get('last_error', 'unknown')[:60]}")
                        print(f"[   FAILED] {step}: {context.get('last_error', 'unknown')[:60]}")
                    
                    if success:
                        previous_results.append(f"{sub_prompt}: {step}")
                        if len(previous_results) > 5:
                            previous_results.pop(0)
                    else:
                        print(f"[⚠️ STEP ERROR] {context['last_error']}")
                        
                        # ===== RETRY WITH ALTERNATIVE STRATEGIES =====
                        print(f"[🔁 RETRY] Step failed, attempting alternative strategies...")
                        retry_success, context = retry_failed_action(step, context)
                        if retry_success:
                            print(f"[✅ RETRY SUCCESS] Alternative strategy worked for: {step}")
                            success = True
                            reason_overlay(f"[DONE] {step} (via retry)")
                        
                        if not success:
                            if "BROWSER_ACTION" in context.get("last_error", ""):
                                print(f"[🔧 AUTO-FIX] Browser action blocked - need to launch browser first")
                                # Try launching browser as recovery
                                if "chrome" in step.lower():
                                    success, context = execute_macro_step("LAUNCH chrome", context)
                                    if success:
                                        success2, context = execute_macro_step("SELECT_PROFILE", context)
                                        success = success2
                                elif "edge" in step.lower():
                                    success, context = execute_macro_step("PRESS win", context)
                                    if success:
                                        success2, context = execute_macro_step("TYPE Edge", context)
                                        if success2:
                                            success3, context = execute_macro_step("PRESS enter", context)
                                            success = success3
                    
                    # Reset retry counter on success
                    context["_retry_attempt"] = 0
                    
                    # Auto-inject SELECT_PROFILE after LAUNCH chrome
                    if step.lower().strip() == "launch chrome" and success:
                        print(f"[👤 AUTO] Injecting SELECT_PROFILE after Chrome launch")
                        reason_overlay(f"[AUTO] Chrome launched - selecting profile...")
                        success_profile, context = execute_macro_step("SELECT_PROFILE", context)
                        if not success_profile:
                            print(f"[⚠️ AUTO] Profile selection failed, continuing...")
                    
                    # Brief check between steps (skip if WAITING is disabled)
                    if not get_skip_waits():
                        time.sleep(0.3)
            
            print(f"[✅ COMPLETE] All {len(sub_tasks)} sub-tasks executed.")
            reason_overlay(f"[DONE] Completed {len(sub_tasks)} sub-tasks\nTask: {prompt[:60]}")
            _add_conversation("AI", f"Task complete. {len(sub_tasks)} sub-tasks executed.", meta={
                "window": context.get("screen_active_window", ""),
                "screen_summary": context.get("screen_summary", ""),
                "steps": sub_tasks,
                "type": "task_complete"
            })
        except ChatReply as cr:
            reply_text = cr.reply_text
            print(f"[💬 AI] {reply_text}")
            reason_overlay(f"[💬 AI]\n{reply_text}")
            embedded_actions = _extract_actions_from_reply(reply_text)
            if embedded_actions:
                print(f"[🔧 EMBEDDED ACTIONS] Executing: {embedded_actions}")
                exec_ctx = {"workspace": os.getcwd(), "retry_count": 0, "skip_waits": False, "_retry_attempt": 0}
                for action in embedded_actions:
                    success, exec_ctx = execute_macro_step(action, exec_ctx)
                    if not success:
                        print(f"[⚠️ EMBEDDED ACTION FAILED] {exec_ctx.get('last_error', 'unknown')}")
                        break
            if any(kw in reply_text.lower() for kw in ["would you like me to proceed", "would you like me to", "shall i proceed", "shall i", "confirm", "verify", "is that correct", "is this correct", "proceed with", "go ahead"]):
                _set_pending_verification(task_prompt)
        except Exception as e:
            print(f"[❌ ERROR] {e}")
            _add_conversation("System", f"Error: {str(e)[:120]}", meta={
                "window": context.get("screen_active_window", ""),
                "error": str(e)[:120],
                "type": "error"
            })
    
    thread = threading.Thread(target=execute, daemon=True)
    thread.start()

def start_hotkey_listener():
    """Start listening for hotkeys."""
    import keyboard
    
    def on_quick_prompt():
        if not getattr(show_quick_prompt, "_showing", False):
            show_quick_prompt._showing = True
            show_quick_prompt()
            show_quick_prompt._showing = False
    
    def on_pause():
        global _paused
        _paused = not _paused
        if _paused:
            print("[⏸ PAUSED] Press Ctrl+P to resume")
    
    keyboard.add_hotkey("ctrl+m", on_quick_prompt)
    keyboard.add_hotkey("ctrl+p", on_pause)
    print("[🔑 HOTKEY] Ctrl+M: Quick prompt | Ctrl+P: Pause")
    return keyboard

if __name__ == "__main__":
    import threading
    
    keyboard_module = start_hotkey_listener()
    
    try:
        run_chat_gui()
    finally:
        keyboard_module.unhook_all_hotkeys()