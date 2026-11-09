"""Strict, deterministic command layer.

Voice transcripts are untrusted and famously prone to hallucinating prose.
Every transcript and typed prompt is passed through this matcher *before* any
LLM is allowed to see it. A high-confidence match is executed locally, which
also provides the "no garbage listening" safety layer for the STT module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_COMMAND_CHARS = 12_000


@dataclass(slots=True)
class Command:
    kind: str
    argument: str = ""
    raw: str = ""
    confidence: float = 1.0
    target: str = ""
    extra: str = ""

    @property
    def destructive(self) -> bool:
        return self.kind in {"shutdown", "restart", "sleep", "lock", "quit"}


_WAKE_PREFIX = re.compile(
    r"^(?:hey\s+|ok(?:ay)?\s+)?blaze(?:\s+ai)?\s*[,:\-]?\s*", re.IGNORECASE
)
_POLITE_PREFIX = re.compile(
    r"^(?:please\s+|kindly\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|"
    r"will\s+you\s+|i\s+want\s+you\s+to\s+)",
    re.IGNORECASE,
)
_POLITE_SUFFIX = re.compile(r"\s+(?:please|for\s+me|now)$", re.IGNORECASE)

_SCREEN_START = re.compile(
    r"^(?:start|begin|launch)\s+(?:a\s+|the\s+)?(?:screen\s+)?"
    r"(?:recording|record|screen\s*capture)$"
    r"|^(?:start|begin)\s+(?:recording|capturing)\s+(?:the\s+|my\s+)?screen$"
    r"|^(?:record|capture)\s+(?:the\s+|my\s+)?screen$",
    re.IGNORECASE,
)
_SCREEN_STOP = re.compile(
    r"^(?:stop|end|finish|save|halt)\s+(?:the\s+|my\s+)?(?:screen\s+)?"
    r"(?:recording|record|capture)$"
    r"|^(?:stop|end|finish)\s+(?:recording|screen\s*recording)$",
    re.IGNORECASE,
)
_OPEN_APP = re.compile(
    r"^(?:open|launch|start|run|fire\s+up|switch\s+to|focus|bring\s+up|show)\s+(?:the\s+|my\s+)?"
    r"(?:app\s+|application\s+|program\s+)?(?P<app>.+?)$",
    re.IGNORECASE,
)
# Browser tab / URL commands. These must be checked before _OPEN_APP, which
# would otherwise treat "new tab in brave" as an application name.
_NEW_TAB = re.compile(
    r"^(?:(?:open|create|make|add)\s+(?:me\s+)?(?:a\s+|the\s+)?(?:new\s+)?tab"
    r"|new\s+tab)"
    r"(?:\s+(?:in|on|with)\s+(?:the\s+)?(?P<app>.+?))?$",
    re.IGNORECASE,
)
_URL_PART = (
    r"(?:(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}"
    r"(?::\d{2,5})?(?:/[^\s]*)?"
    r"|localhost(?::\d{2,5})?(?:/[^\s]*)?)"
)
_OPEN_URL = re.compile(
    r"^(?:open|go\s+to|visit|browse)\s+(?:the\s+)?"
    r"(?:(?:website|site|page|link)\s+)?(?P<url>" + _URL_PART + r")"
    r"(?:\s+(?:in|on|with)\s+(?:the\s+)?(?P<app>.+?))?$",
    re.IGNORECASE,
)
_OPEN_THIS_SITE = re.compile(
    r"^(?:open|show|display|view)\s+(?:this|that|the)\s+"
    r"(?:website|site|page|link|url|webpage)"
    r"(?:\s+(?:in|on|with)\s+(?:the\s+)?(?P<app>.+?))?$",
    re.IGNORECASE,
)
_CLOSE_APP = re.compile(
    r"^(?:close|quit|kill|exit|terminate|force\s+quit)\s+(?:the\s+|my\s+)?"
    r"(?:app\s+|application\s+|program\s+)?(?P<app>.+?)$",
    re.IGNORECASE,
)
_RESTART = re.compile(
    r"^(?:restart|reboot|power\s*cycle)\s*(?:the\s+|my\s+|this\s+)?"
    r"(?:computer|pc|mac|system|machine)?$"
    r"|^(?:can\s+you\s+)?(?:restart|reboot)\s+(?:the\s+)?"
    r"(?:computer|pc|mac|system|machine)$",
    re.IGNORECASE,
)
_SHUTDOWN = re.compile(
    r"^(?:shut\s*down|power\s*off|turn\s*off)\s*(?:the\s+|my\s+|this\s+)?"
    r"(?:computer|pc|mac|system|machine)?$"
    r"|^(?:shut\s*down|power\s*off|turn\s*off)\s+(?:now|completely)$"
    r"|^(?:shut|power|turn)\s+(?:the\s+)?(?:computer|pc|mac|system|machine)\s+"
    r"(?:down|off)$",
    re.IGNORECASE,
)
_SLEEP = re.compile(
    r"^(?:sleep|suspend)\s*(?:the\s+|my\s+|this\s+)?"
    r"(?:computer|pc|mac|system|machine)?$",
    re.IGNORECASE,
)
_LOCK = re.compile(
    r"^(?:lock|secure)\s*(?:the\s+|my\s+|this\s+)?"
    r"(?:computer|pc|mac|screen|system)?$",
    re.IGNORECASE,
)
_TYPE_TEXT = re.compile(
    r"^(?:type(?:\s+out)?|paste|insert)\s+(?:this\s+)?(?:text\s+)?(?P<text>.+)$",
    re.IGNORECASE,
)
_SCREENSHOT = re.compile(
    r"^(?:take|grab|save)\s+(?:a\s+)?screenshot$|^screenshot$"
    r"|^(?:take|grab)\s+(?:a\s+)?screen\s+(?:shot|grab)$",
    re.IGNORECASE,
)
_SITE_NAMES = {
    "youtube": "https://www.youtube.com",
    "you tube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com",
    "linkedin": "https://www.linkedin.com",
    "spotify": "https://open.spotify.com",
    "whatsapp": "https://web.whatsapp.com",
    "chatgpt": "https://chatgpt.com",
    "amazon": "https://www.amazon.com",
    "netflix": "https://www.netflix.com",
    "maps": "https://maps.google.com",
    "drive": "https://drive.google.com",
    "docs": "https://docs.google.com",
    "wikipedia": "https://www.wikipedia.org",
    "youtube music": "https://music.youtube.com",
    "yt music": "https://music.youtube.com",
    "soundcloud": "https://soundcloud.com",
    "apple music": "https://music.apple.com",
}

# Platforms that represent "search and play media" intents (not generic site opens).
_MEDIA_PLATFORMS = {
    "music": "music",
    "apple music": "apple_music",
    "applemusic": "apple_music",
    "itunes": "itunes",
    "spotify": "spotify",
    "youtube": "youtube",
    "you tube": "youtube",
    "yt": "youtube",
    "youtube music": "youtube_music",
    "yt music": "youtube_music",
    "ytmusic": "youtube_music",
    "soundcloud": "soundcloud",
    "amazon music": "amazon_music",
    "prime music": "amazon_music",
    "tidal": "tidal",
    "deezer": "deezer",
    "jiosaavn": "jiosaavn",
    "gaana": "gaana",
    "wynk": "wynk",
}


def _split_play_platform(text: str) -> tuple[str, str]:
    """Return ``(query, platform_key)`` for a play request.

    Recognizes trailing ``on/in/using/with <platform>``. Unknown trailing targets
    are kept in the query to avoid misparsing phrases like "play songs on repeat".
    """

    value = (text or "").strip().strip("\"'")
    if not value:
        return "", ""
    match = re.search(
        r"^(?P<query>.+?)\s+(?:in|on|using|with)\s+(?:the\s+)?(?P<platform>[a-z0-9][a-z0-9 ._-]{0,40})\s*$",
        value,
        re.IGNORECASE,
    )
    if match:
        query = (match.group("query") or "").strip().strip("\"'")
        platform = (match.group("platform") or "").strip().lower().strip("\"' .")
        platform = re.sub(r"\s+", " ", platform)
        key = _MEDIA_PLATFORMS.get(platform)
        if key and query:
            return query, key
        # Browser-app target like "play X on youtube in brave" is handled by caller
        # via separate parsing; keep text intact for generic phrases.
    return value, ""
# "open Brave and open youtube(.com)?", "open chrome then go to github.com"
_OPEN_APP_AND_URL = re.compile(
    r"^(?:open|launch|start)\s+(?:the\s+)?(?P<app>.+?)\s+"
    r"(?:and|then|after\s+that)\s+"
    r"(?:(?:open|go\s+to|visit|browse|search(?:\s+for)?)\s+)?"
    r"(?P<site>" + _URL_PART + r"|[a-z0-9][a-z0-9 ._-]{1,40})"
    r"\s*$",
    re.IGNORECASE,
)
# "play seedhe maut", "play diljit on youtube", "search for/and play X on youtube"
_PLAY_MEDIA = re.compile(
    r"^(?P<action>"
    r"play"
    r"|search(?:\s+for)?(?:\s+and\s+play)?"
    r"|find(?:\s+and\s+play)?"
    r"|listen\s+to"
    r")\s+(?P<query>.+?)\s*$",
    re.IGNORECASE,
)
# "open music and play seedhe maut" / "open spotify and play ..."
_OPEN_AND_PLAY = re.compile(
    r"^(?:open|launch|start)\s+(?:the\s+)?(?P<app>apple\s*music|music|spotify|itunes)\s+"
    r"(?:and|then)\s+(?:play|search(?:\s+for)?(?:\s+and\s+play)?|find(?:\s+and\s+play)?)\s+"
    r"(?P<query>.+?)\s*$",
    re.IGNORECASE,
)
# "open Brave/Chrome and play/search X on YouTube" (browser + playback intent)
_OPEN_BROWSER_AND_PLAY = re.compile(
    r"^(?:open|launch|start)\s+(?:the\s+)?(?P<browser>.+?)\s+"
    r"(?:and|then|after\s+that)\s+"
    r"(?:play|search(?:\s+for)?(?:\s+and\s+play)?|find(?:\s+and\s+play)?)\s+"
    r"(?P<query>.+?)\s*$",
    re.IGNORECASE,
)
_SYSTEM_INFO = re.compile(
    r"^(?:system|resource|ram|memory|cpu|battery)\s+"
    r"(?:status|info|information|usage|report|level|remaining)$"
    r"|^(?:how\s+much\s+)?(?:ram|memory)\s+(?:is\s+)?"
    r"(?:being\s+)?used$"
    r"|^how\s+much\s+(?:ram|memory)\s+(?:do\s+i\s+have|is\s+left)$"
    r"|^(?:check|show)\s+(?:the\s+)?(?:ram|memory|cpu|battery|system)"
    r"(?:\s+usage|\s+status|\s+info)?$"
    r"|^battery(?:\s+level|\s+status)?$"
    r"|^cpu(?:\s+usage)?$",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"^(?:what\s+time\s+is\s+it|what's\s+the\s+time|current\s+time|"
    r"tell\s+me\s+the\s+time|time)$",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"^(?:what(?:'s|\s+is)\s+(?:the\s+)?date|what\s+day\s+is\s+it|"
    r"today's\s+date|current\s+date|date)$",
    re.IGNORECASE,
)
_HELP = re.compile(
    r"^(?:help|commands|capabilities|what\s+can\s+you\s+do|"
    r"show\s+(?:me\s+)?commands|how\s+do\s+i\s+use\s+you)$",
    re.IGNORECASE,
)
_QUIT = re.compile(
    r"^(?:quit|exit|close|shut\s+down)\s+(?:blaze(?:\s+ai)?|yourself|the\s+app)$"
    r"|^(?:quit\s+blaze|exit\s+blaze|close\s+blaze)$",
    re.IGNORECASE,
)
_CONFIRM = re.compile(
    r"^(?:yes|yeah|yep|confirm|do\s+it|go\s+ahead|proceed|affirmative)"
    r"(?:\s+(?P<action>shutdown|shut\s+down|restart|reboot|sleep|lock|send\s+message|message))?$",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"^(?:no|nope|cancel|never\s*mind|abort|stop|forget\s+it)$", re.IGNORECASE
)
# Offline self-learning commands.
_LEARN_ALIAS = re.compile(
    r"^(?:remember(?:\s+that)?|learn(?:\s+that)?|set\s+alias)\s+"
    r"(?:(?:when\s+i\s+say|if\s+i\s+say|saying)\s+)?"
    r"(?P<phrase>.+?)\s+"
    r"(?:means?|=|as|to|should\s+(?:open|launch|start|run|play)|opens?|launches?|starts?)\s+"
    r"(?P<app>.+?)$",
    re.IGNORECASE,
)
_FORGET_ALIAS = re.compile(
    r"^(?:forget|remove|delete)\s+(?:the\s+)?(?:alias|memory|remembered\s+app)\s+"
    r"(?:for\s+)?(?P<phrase>.+?)$",
    re.IGNORECASE,
)
_LEARNING_STATUS = re.compile(
    r"^(?:what\s+have\s+you\s+learned|show\s+(?:your\s+)?(?:learning\s+)?memory|"
    r"learning\s+(?:memory|status)|show\s+aliases|what\s+do\s+you\s+remember)$",
    re.IGNORECASE,
)

# Media playback transport controls (Siri / Google Assistant style)
_MEDIA_PAUSE = re.compile(
    r"^(?:pause|pause\s+music|pause\s+playback|pause\s+song|pause\s+the\s+music|pause\s+the\s+song)$",
    re.IGNORECASE,
)
_MEDIA_RESUME = re.compile(
    r"^(?:resume|resume\s+music|resume\s+playback|unpause|unpause\s+music|continue\s+music|continue\s+playing)$",
    re.IGNORECASE,
)
_MEDIA_STOP = re.compile(
    r"^(?:stop\s+music|stop\s+playback|stop\s+playing|halt\s+music)$",
    re.IGNORECASE,
)
_MEDIA_NEXT = re.compile(
    r"^(?:next\s+song|next\s+track|next|skip|skip\s+song|skip\s+track|skip\s+this\s+song)$",
    re.IGNORECASE,
)
_MEDIA_PREV = re.compile(
    r"^(?:previous\s+song|previous\s+track|prev\s+song|prev\s+track|last\s+song|back\s+track|go\s+back\s+a\s+song)$",
    re.IGNORECASE,
)
_VOLUME_UP = re.compile(
    r"^(?:volume\s+up|turn\s+it\s+up|louder|increase\s+volume|make\s+it\s+louder)$",
    re.IGNORECASE,
)
_VOLUME_DOWN = re.compile(
    r"^(?:volume\s+down|turn\s+it\s+down|softer|decrease\s+volume|lower\s+volume|quieter|make\s+it\s+quieter)$",
    re.IGNORECASE,
)
_VOLUME_MUTE = re.compile(
    r"^(?:mute|unmute|silence|mute\s+volume|toggle\s+mute|unmute\s+volume)$",
    re.IGNORECASE,
)
_VOLUME_SET = re.compile(
    r"^(?:set\s+volume\s+to|volume)\s+(?P<level>\d{1,3})\s*%?$",
    re.IGNORECASE,
)

# Contacts & Messaging controls
_LIST_CONTACTS = re.compile(
    r"^(?:who\s+are\s+my\s+contacts|list\s+contacts|show\s+(?:my\s+)?contacts|contacts|my\s+contacts)$",
    re.IGNORECASE,
)
_DELETE_CONTACT = re.compile(
    r"^(?:delete|remove|forget)\s+(?:the\s+)?contact\s+(?P<name>.+)$",
    re.IGNORECASE,
)
_SAVE_CONTACT = re.compile(
    r"^(?:save|remember|add|set)\s+(?:a\s+)?contact\s+(?P<name>[a-z0-9 _-]{1,35}?)\s+(?:with\s+)?(?:phone|number|mobile)?\s*(?P<phone>\+?[0-9\-\(\)\s]{6,25})?(?:\s*(?:and\s+)?(?:email|mail)\s+(?P<email>[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}))?$",
    re.IGNORECASE,
)
_SAVE_CONTACT_EMAIL_FIRST = re.compile(
    r"^(?:save|remember|add|set)\s+(?:a\s+)?contact\s+(?P<name>[a-z0-9 _-]{1,35}?)\s+(?:with\s+)?(?:email|mail)\s+(?P<email>[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})(?:\s*(?:and\s+)?(?:phone|number|mobile)\s*(?P<phone>\+?[0-9\-\(\)\s]{6,25}))?$",
    re.IGNORECASE,
)
_SEND_WHATSAPP = re.compile(
    r"^(?:send\s+(?:a\s+)?whatsapp(?:\s+message)?\s+to|whatsapp)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_EMAIL = re.compile(
    r"^(?:send\s+(?:an?\s+)?(?:e-?mail|mail)(?:\s+to)?|email|mail)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:(?:about|subject|with\s+subject)\s+(?P<subject>[^:\n]+?)\s+(?:saying|:)|saying|:)?\s*(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_SMS = re.compile(
    r"^(?:send\s+(?:a\s+)?(?:text|message|sms)(?:\s+to)?|text|message|msg)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_TELEGRAM = re.compile(
    r"^(?:send\s+(?:a\s+)?telegram(?:\s+message)?\s+to|telegram)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_SIGNAL = re.compile(
    r"^(?:send\s+(?:a\s+)?signal(?:\s+message)?\s+to|signal)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_IMSG = re.compile(
    r"^(?:send\s+(?:a\s+)?(?:imessage|iMessage|apple\s+message)(?:\s+to)?|imessage)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_FACEBOOK = re.compile(
    r"^(?:send\s+(?:a\s+)?(?:facebook|messenger)(?:\s+message)?\s+to|facebook|messenger)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_SKYPE = re.compile(
    r"^(?:send\s+(?:a\s+)?skype(?:\s+message)?\s+to|skype)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_THREEMA = re.compile(
    r"^(?:send\s+(?:a\s+)?threema(?:\s+message)?\s+to|threema)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)
_SEND_MESSAGE_CHANNEL = re.compile(
    r"^(?:send\s+(?:a\s+)?message\s+to|message)\s+(?P<recipient>[^:\s,]+(?:\s+[^:\s,]+)?)\s+(?:on|via|using)\s+(?P<channel>whatsapp|telegram|signal|imessage|facebook|skype|threema|sms|email)\s*(?:saying|:|telling\s+them)?\s+(?P<message>.+)$",
    re.IGNORECASE,
)

# Download a file from a direct URL, or search for something to download.
_DOWNLOAD_FILE = re.compile(
    r"^(?:download\s+(?:the\s+)?(?:file\s+)?(?:from\s+)?|fetch\s+file\s+)(?P<url>https?://[^\s]+)(?:\s+to\s+(?P<target>.+))?$",
    re.IGNORECASE,
)
_DOWNLOAD_SEARCH = re.compile(
    r"^(?:download|fetch)\s+(?P<query>[^\s][^:]{1,120}?)(?:\s+to\s+(?P<target>.+))?$",
    re.IGNORECASE,
)

# Web Search & Safe Downloading
_SEARCH_ENGINES_PATTERN = r"(?:google|duckduckgo|ddg|bing|yahoo|wikipedia|wiki|youtube|github|reddit)"
_WEB_SEARCH_EXPLICIT = re.compile(
    r"^(?:search\s+(?P<engine>" + _SEARCH_ENGINES_PATTERN + r")\s+for\s+(?P<query1>.+)|search\s+(?:for\s+)?(?P<query2>.+?)\s+on\s+(?P<engine2>" + _SEARCH_ENGINES_PATTERN + r")|(?P<engine3>google|duckduckgo|bing|wikipedia)\s+(?P<query3>.+))\s*$",
    re.IGNORECASE,
)
_WEB_SEARCH_GENERIC = re.compile(
    r"^(?:search(?:\s+the\s+web|\s+online|\s+internet)?\s+for|look\s+up(?:\s+on\s+the\s+web|\s+online)?|web\s+search)\s+(?P<query>.+)$",
    re.IGNORECASE,
)
_DOWNLOAD_FILE = re.compile(
    r"^(?:download\s+(?:the\s+)?(?:file\s+)?(?:from\s+)?|fetch\s+file\s+)(?P<url>https?://[^\s]+)(?:\s+to\s+(?P<target>.+))?$",
    re.IGNORECASE,
)


def _clean_for_matching(text: str) -> str:
    value = text.strip()
    value = _WAKE_PREFIX.sub("", value)
    value = _POLITE_PREFIX.sub("", value)
    value = _POLITE_SUFFIX.sub("", value)
    value = value.strip().strip("?.!,;:")
    return value.strip()


def _valid_app_argument(argument: str, *, voice: bool) -> bool:
    value = argument.strip().strip("\"'")
    if not value or len(value) > 80:
        return False
    # Prevent conversational prose from being mistaken for an app name.
    if len(value.split()) > (7 if voice else 10):
        return False
    if value.lower() in {"it", "this", "that", "something", "a", "the"}:
        return False
    return True


def _clean_typed_text(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def match_command(text: str, *, voice: bool = False) -> Command | None:
    """Return a local command for a prompt, or ``None`` for natural language.

    ``voice=True`` applies tighter app-name limits because STT transcripts are
    more likely to contain a wandering sentence beginning with "open".
    """

    if not text or len(text) > MAX_COMMAND_CHARS:
        return None
    original = text.strip()
    if not original:
        return None
    cleaned = _clean_for_matching(original)
    if not cleaned:
        return None

    # Ordering matters: "start recording" must not become "open app recording".
    if _SCREEN_START.fullmatch(cleaned):
        return Command("record_start", raw=original)
    if _SCREEN_STOP.fullmatch(cleaned):
        return Command("record_stop", raw=original)
    if _QUIT.fullmatch(cleaned):
        return Command("quit", raw=original)
    if _RESTART.fullmatch(cleaned):
        return Command("restart", raw=original)
    if _SHUTDOWN.fullmatch(cleaned):
        return Command("shutdown", raw=original)
    if _SLEEP.fullmatch(cleaned):
        return Command("sleep", raw=original)
    if _LOCK.fullmatch(cleaned):
        return Command("lock", raw=original)
    if _SCREENSHOT.fullmatch(cleaned):
        return Command("screenshot", raw=original)
    if _HELP.fullmatch(cleaned):
        return Command("help", raw=original)
    if _TIME.fullmatch(cleaned):
        return Command("time", raw=original)
    if _DATE.fullmatch(cleaned):
        return Command("date", raw=original)
    if _SYSTEM_INFO.fullmatch(cleaned):
        return Command("system_info", raw=original)
    if _CONFIRM.fullmatch(cleaned):
        action = _CONFIRM.fullmatch(cleaned).group("action") or ""  # type: ignore[union-attr]
        return Command("confirm", re.sub(r"\s+", "_", action.lower()), original)
    if _CANCEL.fullmatch(cleaned):
        return Command("cancel", raw=original)
    if _LEARNING_STATUS.fullmatch(cleaned):
        return Command("learning_status", raw=original)

    # Media playback transport controls (Siri / Google Assistant)
    if _MEDIA_PAUSE.fullmatch(cleaned):
        return Command("media_pause", raw=original)
    if _MEDIA_RESUME.fullmatch(cleaned):
        return Command("media_resume", raw=original)
    if _MEDIA_STOP.fullmatch(cleaned):
        return Command("media_stop", raw=original)
    if _MEDIA_NEXT.fullmatch(cleaned):
        return Command("media_next", raw=original)
    if _MEDIA_PREV.fullmatch(cleaned):
        return Command("media_prev", raw=original)
    if _VOLUME_UP.fullmatch(cleaned):
        return Command("volume_up", raw=original)
    if _VOLUME_DOWN.fullmatch(cleaned):
        return Command("volume_down", raw=original)
    if _VOLUME_MUTE.fullmatch(cleaned):
        return Command("volume_mute", raw=original)
    match = _VOLUME_SET.fullmatch(cleaned)
    if match:
        lvl = match.group("level")
        return Command("volume_set", lvl, original)

    # Contacts management
    if _LIST_CONTACTS.fullmatch(cleaned):
        return Command("list_contacts", raw=original)
    match = _DELETE_CONTACT.fullmatch(cleaned)
    if match:
        name = _clean_typed_text(match.group("name") or "")
        if name:
            return Command("delete_contact", name, original)
    match = _SAVE_CONTACT.fullmatch(cleaned)
    if match:
        name = _clean_typed_text(match.group("name") or "")
        phone = (match.group("phone") or "").strip()
        email = (match.group("email") or "").strip()
        if name and (phone or email):
            return Command("save_contact", name, original, target=phone, extra=email)
    match = _SAVE_CONTACT_EMAIL_FIRST.fullmatch(cleaned)
    if match:
        name = _clean_typed_text(match.group("name") or "")
        email = (match.group("email") or "").strip()
        phone = (match.group("phone") or "").strip()
        if name and (email or phone):
            return Command("save_contact", name, original, target=phone, extra=email)

    # Contact messaging (WhatsApp, Email, SMS/iMessage)
    match = _SEND_WHATSAPP.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="whatsapp", extra=recipient)

    match = _SEND_EMAIL.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        subj = _clean_typed_text(match.group("subject") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="email", extra=f"{recipient}|{subj}")

    match = _SEND_SMS.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="sms", extra=recipient)

    # Generic channel-first: "message X on telegram saying Y" / "message Y on whatsapp to X"
    match = _SEND_MESSAGE_CHANNEL.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        channel = (match.group("channel") or "sms").lower()
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target=channel, extra=recipient)

    match = _SEND_TELEGRAM.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="telegram", extra=recipient)

    match = _SEND_SIGNAL.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="signal", extra=recipient)

    match = _SEND_IMSG.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="imessage", extra=recipient)

    match = _SEND_FACEBOOK.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="facebook", extra=recipient)

    match = _SEND_SKYPE.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="skype", extra=recipient)

    match = _SEND_THREEMA.fullmatch(cleaned)
    if match:
        recipient = _clean_typed_text(match.group("recipient") or "")
        msg = _clean_typed_text(match.group("message") or "")
        if recipient and msg:
            return Command("send_message", msg, original, target="threema", extra=recipient)

    # Safe web download from a direct URL
    match = _DOWNLOAD_FILE.fullmatch(cleaned)
    if match:
        url = (match.group("url") or "").strip()
        dest = _clean_typed_text(match.group("target") or "")
        if url:
            return Command("download_file", url, original, target=dest)

    # Download by search query (not a direct URL) — opens search results and guides the user
    match = _DOWNLOAD_SEARCH.fullmatch(cleaned)
    if match:
        query = _clean_typed_text(match.group("query") or "")
        dest = _clean_typed_text(match.group("target") or "")
        if query:
            # Avoid treating app-like commands as download queries.
            if query.lower().strip() in {"it", "this", "that", "something", "a", "the"}:
                pass
            else:
                return Command("download_file", query, original, target=dest)

    match = _FORGET_ALIAS.fullmatch(cleaned)
    if match:
        phrase = _clean_typed_text(match.group("phrase") or "")
        if phrase:
            return Command("forget_alias", phrase, original)
    match = _LEARN_ALIAS.fullmatch(cleaned)
    if match:
        phrase = _clean_typed_text(match.group("phrase") or "").strip(" ,:;.-")
        app = _clean_typed_text(match.group("app") or "")
        app = re.sub(r"^(?:open|launch|start|run|play)\s+", "", app, flags=re.I).strip()
        if phrase and app and len(phrase.split()) <= 8 and len(app.split()) <= 10:
            return Command("learn_alias", phrase, original, target=app)

    match = _TYPE_TEXT.fullmatch(cleaned)
    if match:
        typed = _clean_typed_text(match.group("text"))
        if typed:
            return Command("type_text", typed, original)

    # Media playback/search: deterministic, offline-capable where possible.
    # "open Brave/Chrome and play/search X on YouTube/Music/..." (browser + media)
    match = _OPEN_BROWSER_AND_PLAY.fullmatch(cleaned)
    if match:
        browser = (match.group("browser") or "").strip().strip("\"'")
        raw_query = _clean_typed_text(match.group("query") or "")
        query, platform = _split_play_platform(raw_query)
        # Avoid mis-routing "open music and play..." here.
        if browser.lower() not in {"music", "apple music", "spotify", "itunes"} and query:
            if not platform:
                # "open Brave and play X" -> search+play on YouTube by default in browser.
                platform = "youtube"
            if _valid_app_argument(browser, voice=voice) or not voice:
                return Command("play_media", query, original, target=platform, extra=browser)

    # "open Music/Spotify/iTunes and play/search X"
    match = _OPEN_AND_PLAY.fullmatch(cleaned)
    if match:
        app = (match.group("app") or "Music").strip().strip("\"'")
        raw_query = _clean_typed_text(match.group("query") or "")
        query, platform = _split_play_platform(raw_query)
        if not query:
            query = raw_query
        if query:
            # If user said "open Music and play X on YouTube", prefer the explicit platform.
            target = platform or _MEDIA_PLATFORMS.get(app.lower().strip(), app)
            return Command("play_media", query, original, target=target, extra=app)

    match = _PLAY_MEDIA.fullmatch(cleaned)
    if match:
        action = (match.group("action") or "").lower()
        raw_query = _clean_typed_text(match.group("query") or "")
        query, platform = _split_play_platform(raw_query)
        if not query:
            query = raw_query
        lowered = query.lower().strip()
        has_play_intent = "play" in action or "listen" in action or bool(platform)
        if has_play_intent and query:
            if lowered in {"music", "song", "a song", "some music", "the music", "my music", "some songs", "tunes"}:
                target = platform or "music"
                return Command("play_media", "music", original, target=target)
            if lowered not in {"pause", "resume", "next", "previous", "stop", "it", "this", "that", "something"}:
                target = platform or "music"
                return Command("play_media", query, original, target=target)

    # Web search (checked after media so "search and play X on YouTube" routes to play_media)
    match = _WEB_SEARCH_EXPLICIT.fullmatch(cleaned)
    if match:
        engine = (match.group("engine") or match.group("engine2") or match.group("engine3") or "google").lower()
        query = _clean_typed_text(match.group("query1") or match.group("query2") or match.group("query3") or "")
        if query:
            return Command("web_search", query, original, target=engine)

    match = _WEB_SEARCH_GENERIC.fullmatch(cleaned)
    if match:
        query = _clean_typed_text(match.group("query") or "")
        if query:
            return Command("web_search", query, original, target="google")

    # Compound open + URL/site (offline-capable) before greedy open_app.
    match = _OPEN_APP_AND_URL.fullmatch(cleaned)
    if match:
        app = (match.group("app") or "").strip().strip("\"'")
        site = (match.group("site") or "").strip().strip("\"'")
        site = re.sub(r"\s+(?:after\s+that|afterwards|please|now)$", "", site, flags=re.I).strip()
        resolved = _SITE_NAMES.get(site.lower().strip().rstrip("?.!,"))
        if not resolved:
            from .app_finder import normalize_web_url

            resolved = normalize_web_url(site)
        if app and resolved and _valid_app_argument(app, voice=voice):
            return Command("open_url", resolved, original, target=app)

    # "open youtube in brave" / "open github in chrome" (known site names)
    match = re.fullmatch(
        r"(?:open|go\s+to|visit|browse)\s+(?P<site>[a-z0-9][a-z0-9 ._-]{1,30})\s+"
        r"(?:in|on|with)\s+(?:the\s+)?(?P<app>.+)",
        cleaned,
        re.IGNORECASE,
    )
    if match:
        site = (match.group("site") or "").strip().strip("\"'")
        app = (match.group("app") or "").strip().strip("\"'")
        resolved = _SITE_NAMES.get(site.lower().strip().rstrip("?.!,"))
        if not resolved:
            from .app_finder import normalize_web_url

            resolved = normalize_web_url(site)
        if resolved and app and _valid_app_argument(app, voice=voice):
            return Command("open_url", resolved, original, target=app)

    # Browser tab/URL intent wins over app launching.
    match = _NEW_TAB.fullmatch(cleaned)
    if match:
        app = (match.group("app") or "").strip().strip("\"'")
        return Command("new_tab", app, original)

    match = _OPEN_URL.fullmatch(cleaned)
    if match:
        app = (match.group("app") or "").strip().strip("\"'")
        url = match.group("url")
        resolved = _SITE_NAMES.get(url.lower().strip().rstrip("?.!,"))
        if not resolved:
            from .app_finder import normalize_web_url

            resolved = normalize_web_url(url) or url
        return Command("open_url", resolved, original, target=app)

    match = _OPEN_THIS_SITE.fullmatch(cleaned)
    if match:
        app = (match.group("app") or "").strip().strip("\"'")
        return Command("open_url", "", original, target=app)

    match = _CLOSE_APP.fullmatch(cleaned)
    if match:
        app = match.group("app").strip().strip("\"'")
        if app.lower() in {"blaze", "blaze ai", "yourself", "the app"}:
            return Command("quit", raw=original)
        if _valid_app_argument(app, voice=voice):
            app = re.sub(r"\s+(?:app|application|program)$", "", app, flags=re.I)
            return Command("close_app", app, original)

    match = _OPEN_APP.fullmatch(cleaned)
    if match:
        app = match.group("app").strip().strip("\"'")
        if app.lower() in {"settings", "preferences", "the settings"}:
            return Command("ui_settings", raw=original)
        if _valid_app_argument(app, voice=voice):
            app = re.sub(r"\s+(?:app|application|program)$", "", app, flags=re.I)
            return Command("open_app", app, original)
    return None


def is_command(text: str, *, voice: bool = False) -> bool:
    return match_command(text, voice=voice) is not None
