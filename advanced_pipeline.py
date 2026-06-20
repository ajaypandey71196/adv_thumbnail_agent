import io
import os
import re
import time
import json
import hashlib
import math
import random
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from groq import Groq
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter, ImageOps

# --- CONFIGURATION ---
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "credentials.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1Q6xW-o_khLb13dX7kfKLZ0_0LCZ7aUXIrtfygm_szyY")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/app/output")
REFERENCE_DIR = os.getenv("REFERENCE_DIR", "assets/references")
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")
POLLINATIONS_TIMEOUT = int(os.getenv("POLLINATIONS_TIMEOUT", "90"))
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "30"))
USE_REMBG = os.getenv("USE_REMBG", "true").lower() in ("1", "true", "yes")
BRAND_TAGLINE = os.getenv("BRAND_TAGLINE", "BY EXPERT FACULTY")

THUMB_W, THUMB_H = 1920, 1080

# ── Person: taller, shifted right, slightly higher up for drama
PERSON_SIZE = (860, 920)
PERSON_POS  = (1040, 90)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ── Custom font paths (downloaded by Dockerfile into /usr/share/fonts/custom/)
CUSTOM_FONT_DIR = "/usr/share/fonts/custom"
FONT_TITLE_PATH  = os.path.join(CUSTOM_FONT_DIR, "Montserrat-ExtraBold.ttf")
FONT_BODY_PATH   = os.path.join(CUSTOM_FONT_DIR, "Montserrat-Bold.ttf")
FONT_BADGE_PATH  = os.path.join(CUSTOM_FONT_DIR, "Montserrat-ExtraBold.ttf")

# Fallback to system fonts if custom not present
FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

try:
    from rembg import remove as rembg_remove
except ImportError:
    rembg_remove = None

# ── AI image enhancers (optional — graceful fallback if not installed)
USE_ENHANCER   = os.getenv("USE_ENHANCER", "true").lower() in ("1", "true", "yes")
ENHANCER_SCALE = int(os.getenv("ENHANCER_SCALE", "2"))   # 2=fast, 4=max quality

try:
    from gfpgan import GFPGANer
    _gfpgan_restorer = None  # lazy-loaded on first use

    def _get_gfpgan():
        global _gfpgan_restorer
        if _gfpgan_restorer is None:
            print("🔬 Loading GFPGAN face restorer...", flush=True)
            # model auto-downloaded to ~/.cache on first run
            _gfpgan_restorer = GFPGANer(
                model_path="https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth",
                upscale=ENHANCER_SCALE,
                arch="clean",
                channel_multiplier=2,
                bg_upsampler=None,   # we handle upscaling separately
            )
        return _gfpgan_restorer
    HAS_GFPGAN = True
except ImportError:
    HAS_GFPGAN = False

try:
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer
    _realesrgan_upscaler = None  # lazy-loaded on first use

    def _get_realesrgan():
        global _realesrgan_upscaler
        if _realesrgan_upscaler is None:
            print("🔬 Loading Real-ESRGAN upscaler...", flush=True)
            model_name = f"RealESRGAN_x{ENHANCER_SCALE}plus"
            model_url  = f"https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/{model_name}.pth"
            netscale   = ENHANCER_SCALE
            model = RRDBNet(
                num_in_ch=3, num_out_ch=3,
                num_feat=64, num_block=23, num_grow_ch=32, scale=netscale,
            )
            _realesrgan_upscaler = RealESRGANer(
                scale=netscale,
                model_path=model_url,
                model=model,
                tile=256,     # tiled inference keeps RAM low on CPU
                tile_pad=10,
                pre_pad=0,
                half=False,   # False = CPU-safe (no fp16 needed)
            )
        return _realesrgan_upscaler
    HAS_REALESRGAN = True
except ImportError:
    HAS_REALESRGAN = False

LLM_KEYS = (
    "main_title",
    "subtitle",
    "badge",
    "highlight_word",
    "image_prompt",
    "accent_color",
    "highlight_color",
    "subtitle_color",
    "theme",
    "layout_style",
)


# ══════════════════════════════════════════════════════
#  GOOGLE SERVICES
# ══════════════════════════════════════════════════════

def get_google_services():
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPES
    )
    sheet_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheet_service, drive_service


def create_resilient_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=5, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


http_session = create_resilient_session()

# Global rate-limit state for free-tier Pollinations
_last_pollinations_call = 0
_POLLINATIONS_COOLDOWN = 120  # seconds between free-tier requests


# ══════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════

def extract_drive_id(url):
    if not url:
        return None
    match = re.search(r"(?:id=|\/d\/)([a-zA-Z0-9-_]+)", str(url))
    return match.group(1) if match else None


def prompt_seed(text):
    return int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16) % 2147483646


def parse_hex_color(value, fallback):
    if not value or not isinstance(value, str):
        return fallback
    value = value.strip().lstrip("#")
    if len(value) == 6:
        try:
            return tuple(int(value[i: i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return fallback


def fit_cover(img, width, height):
    img = img.convert("RGBA")
    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return resized.crop((left, top, left + width, top + height))


def trim_transparent(img):
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def rgb_to_hex(rgb_tuple):
    """Convert (r,g,b) tuple to #RRGGBB string."""
    return "#{:02x}{:02x}{:02x}".format(*rgb_tuple)


def blend_colors(c1, c2, alpha):
    """Blend two (r,g,b) colors with alpha 0.0-1.0."""
    return tuple(int(c1[i] * (1 - alpha) + c2[i] * alpha) for i in range(3))


def keyword_color_map(raw_title):
    """
    Returns deterministic accent colors based on keywords in the title.
    Used to *enhance* (not replace) Groq LLM colors.
    Returns: (accent_rgb, highlight_rgb, subtitle_rgb, suggested_theme)
    """
    title_lower = str(raw_title).lower()

    # Each entry: (keywords_tuple, accent_hex, highlight_hex, subtitle_hex, theme)
    KEYWORD_THEMES = [
        # Chemistry
        (("chemistry", "mole", "organic", "inorganic", "elements", "compound",
           "reaction", "acid", "base", "molecular"),
         (139, 31, 166), (255, 215, 0), (255, 180, 50), "chemistry"),

        # Physics
        (("physics", "light", "motion", "force", "energy", "electricity",
           "magnetism", "optics", "mechanics", "waves"),
         (26, 139, 26), (255, 215, 0), (200, 255, 100), "physics"),

        # Maths
        (("math", "maths", "mathematics", "algebra", "calculus", "geometry",
           "trigonometry", "integration", "differentiation", "equation"),
         (204, 0, 0), (255, 102, 0), (255, 150, 50), "maths"),

        # Biology / NEET
        (("biology", "neet", "zoology", "botany", "human", "anatomy",
           "cell", "genetics", "dna", "organic life"),
         (0, 128, 128), (0, 255, 200), (100, 255, 180), "biology"),

        # JEE / Competitive exam
        (("jee", "gate", "ese", "upsc", "ssc", "nda", "cds", "competitive",
           "entrance", "exam prep", "crash course"),
         (139, 0, 0), (255, 215, 0), (255, 180, 50), "exam"),

        # Current Affairs / Update
        (("update", "news", "current", "affairs", "breaking", "latest",
           "announcement", "important", "notice", "alert"),
         (229, 9, 20), (255, 255, 255), (255, 200, 200), "news"),

        # Motivation / Strategy
        (("motivation", "strategy", "tips", "tricks", "hack", "secret",
           "success", "mindset", "discipline", "routine"),
         (255, 140, 0), (255, 255, 0), (255, 220, 100), "motivation"),

        # Technology / Coding
        (("tech", "technology", "coding", "programming", "python", "java",
           "computer", "ai", "software", "development"),
         (0, 100, 200), (0, 200, 255), (100, 220, 255), "technology"),

        # History / Polity
        (("history", "polity", "geography", "economics", "constitution",
           "civics", "political", "ancient", "medieval"),
         (139, 69, 19), (255, 200, 100), (255, 170, 80), "history"),

        # One Shot / Revision
        (("one shot", "revision", "quick", "rapid", "summary", "revision",
           "last minute", "marathon", "complete"),
         (30, 30, 30), (255, 215, 0), (200, 180, 100), "revision"),
    ]

    for keywords, accent, highlight, subtitle, theme in KEYWORD_THEMES:
        if any(kw in title_lower for kw in keywords):
            return accent, highlight, subtitle, theme

    # Default education palette
    return (239, 68, 68), (250, 204, 21), (245, 158, 11), "education"


def generate_noise_texture(width, height, intensity=15, seed=None):
    """
    Generate a grayscale noise texture for background richness.
    Low-intensity noise (default 15) adds subtle film grain.
    """
    if seed is not None:
        random.seed(seed)
    img = Image.new("L", (width, height))
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = random.randint(128 - intensity, 128 + intensity)
    return img.filter(ImageFilter.GaussianBlur(radius=2))


def draw_light_rays(width, height, cx, cy, color, num_rays=14, spread_deg=70):
    """
    Draw procedural light-ray beams emanating from a center point.
    Used for PW-style radiating background glow behind portraits.
    """
    rays = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(rays)
    base_angle = -90 - spread_deg / 2  # Start from upper direction
    step = spread_deg / max(num_rays - 1, 1)

    max_dist = math.sqrt(width ** 2 + height ** 2)

    for i in range(num_rays):
        angle = math.radians(base_angle + i * step)
        # Ray goes from center to edge of canvas
        end_x = cx + math.cos(angle) * max_dist
        end_y = cy + math.sin(angle) * max_dist

        # Vary width and alpha for organic feel
        ray_w = random.randint(20, 50)
        alpha = random.randint(20, 55)

        draw.line(
            [(cx, cy), (end_x, end_y)],
            fill=color + (alpha,),
            width=ray_w,
        )

    # Blur to soften beams
    rays = rays.filter(ImageFilter.GaussianBlur(radius=35))
    return rays


# ══════════════════════════════════════════════════════
#  FONTS  — Montserrat preferred, system fallback
# ══════════════════════════════════════════════════════

def _best_font_path(preferred, fallbacks):
    if os.path.exists(preferred):
        return preferred
    for p in fallbacks:
        if os.path.exists(p):
            return p
    return None


def get_fonts():
    title_path = _best_font_path(FONT_TITLE_PATH, FALLBACK_FONTS)
    body_path  = _best_font_path(FONT_BODY_PATH,  FALLBACK_FONTS)
    badge_path = _best_font_path(FONT_BADGE_PATH, FALLBACK_FONTS)

    if not title_path:
        default = ImageFont.load_default()
        return default, default, default

    return (
        ImageFont.truetype(title_path, 118),   # headline — big and bold
        ImageFont.truetype(body_path,  52),    # subtitle
        ImageFont.truetype(badge_path, 38),    # badge / tagline
    )


# ══════════════════════════════════════════════════════
#  PORTRAIT PROCESSING
# ══════════════════════════════════════════════════════

def remove_light_background(img):
    img = img.convert("RGBA")
    pixels = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            chroma = max(r, g, b) - min(r, g, b)
            if lum > 245 and chroma < 25:
                pixels[x, y] = (r, g, b, 0)
            elif lum > 215 and chroma < 35:
                fade = int((255 - lum) * 12)
                pixels[x, y] = (r, g, b, min(a, max(0, 255 - fade)))


def _rembg_to_rgba(cut):
    if isinstance(cut, Image.Image):
        return cut.convert("RGBA")
    return Image.open(io.BytesIO(cut)).convert("RGBA")


def enhance_portrait_ai(pil_img_rgb):
    """
    Two-step AI enhancement pipeline:
      1. Real-ESRGAN  — upscale + sharpen texture (runs first on small image = fast)
      2. GFPGAN       — restore face details (eyes, skin, teeth)

    Both models are optional. If neither is installed the original image is returned.
    Runs on CPU — takes ~5-20s per image depending on resolution.
    """
    import numpy as np

    if not USE_ENHANCER:
        return pil_img_rgb

    img_np = np.array(pil_img_rgb)   # RGB uint8

    # ── Step 1: Real-ESRGAN upscale
    if HAS_REALESRGAN:
        try:
            upscaler = _get_realesrgan()
            # realesrgan expects BGR
            import cv2
            bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
            out_bgr, _ = upscaler.enhance(bgr, outscale=ENHANCER_SCALE)
            img_np = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2RGB)
            print(f"   ✨ ESRGAN upscaled → {img_np.shape[1]}×{img_np.shape[0]}", flush=True)
        except Exception as e:
            print(f"   ⚠ Real-ESRGAN failed: {e}", flush=True)

    # ── Step 2: GFPGAN face restoration
    if HAS_GFPGAN:
        try:
            import cv2
            restorer = _get_gfpgan()
            bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
            # returns (cropped_faces, restored_faces, output_bgr)
            _, _, restored_bgr = restorer.enhance(
                bgr,
                has_aligned=False,
                only_center_face=False,
                paste_back=True,
                weight=0.5,   # 0=pure GFPGAN, 1=pure original; 0.5 = balanced
            )
            if restored_bgr is not None:
                img_np = cv2.cvtColor(restored_bgr, cv2.COLOR_BGR2RGB)
                print("   ✨ GFPGAN face restoration done", flush=True)
        except Exception as e:
            print(f"   ⚠ GFPGAN failed: {e}", flush=True)

    return Image.fromarray(img_np)


def cutout_portrait(raw_bytes):
    if USE_REMBG and rembg_remove:
        try:
            src = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
            return _rembg_to_rgba(rembg_remove(src))
        except Exception as e:
            print(f"⚠ rembg failed ({e}), using light-bg removal.", flush=True)
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
    remove_light_background(img)
    return img


def add_rim_light(person_rgba, rim_color=(255, 255, 255)):
    """Adds a soft colored rim-light glow around the person silhouette."""
    alpha = person_rgba.split()[3]
    glow = Image.new("RGBA", person_rgba.size, rim_color + (0,))
    edge = alpha.filter(ImageFilter.MaxFilter(9))
    glow.putalpha(edge)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=6))
    base = Image.new("RGBA", person_rgba.size, (0, 0, 0, 0))
    base.paste(glow, (0, 0), glow)
    base.paste(person_rgba, (0, 0), person_rgba)
    return base


def prepare_portrait(raw_bytes, rim_color=(255, 255, 255)):
    try:
        # ── Step 0: AI enhance BEFORE background removal
        #    (enhancers work best on the original full photo, not a cutout)
        raw_pil = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        if USE_ENHANCER and (HAS_GFPGAN or HAS_REALESRGAN):
            print("🎨 Enhancing portrait quality...", flush=True)
            raw_pil = enhance_portrait_ai(raw_pil)
            # Convert back to bytes so cutout_portrait can use its normal flow
            buf = io.BytesIO()
            raw_pil.save(buf, format="PNG")
            raw_bytes = buf.getvalue()

        img = cutout_portrait(raw_bytes)
        img = trim_transparent(img)
    except Exception as e:
        print(f"⚠ prepare_portrait error: {e}", flush=True)
        return Image.new("RGBA", PERSON_SIZE, (0, 0, 0, 0))

    if max(img.size) < 10:
        return Image.new("RGBA", PERSON_SIZE, (0, 0, 0, 0))

    target_h = int(PERSON_SIZE[1] * 0.97)
    scale = target_h / img.size[1]
    new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
    img = img.resize(new_size, Image.Resampling.LANCZOS)

    # ── Colour-tinted rim light matching the accent
    img = add_rim_light(img, rim_color=rim_color)

    canvas = Image.new("RGBA", PERSON_SIZE, (0, 0, 0, 0))
    ox = (PERSON_SIZE[0] - img.size[0]) // 2
    oy = PERSON_SIZE[1] - img.size[1]
    canvas.paste(img, (ox, max(0, oy)), img)
    return canvas


# ══════════════════════════════════════════════════════
#  BACKGROUND
# ══════════════════════════════════════════════════════

def enhance_background(img, accent=None):
    img = fit_cover(img, THUMB_W, THUMB_H)
    rgb = img.convert("RGB")
    rgb = ImageEnhance.Contrast(rgb).enhance(1.20)
    rgb = ImageEnhance.Color(rgb).enhance(1.22)
    rgb = ImageEnhance.Brightness(rgb).enhance(0.88)   # slightly darker = more drama
    rgb = ImageEnhance.Sharpness(rgb).enhance(1.25)
    return rgb.convert("RGBA")


def make_chaotic_gradient_fallback(accent, accent2, raw_title=""):
    """
    PW-style chaotic gradient background with multiple blended layers,
    procedural light rays, noise texture, and vignette.
    Replaces the old simple gradient fallback.
    """
    # Use title hash to make deterministic "random" visuals
    seed = prompt_seed(raw_title or "fallback")
    rng = random.Random(seed)

    # ── Base dark canvas
    base = Image.new("RGB", (THUMB_W, THUMB_H), (5, 6, 12))

    # ── Layer 1: Large radial glow blobs (3-5 overlapping circles)
    blobs = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(blobs)
    blob_colors = [
        accent + (rng.randint(60, 100),),
        accent2 + (rng.randint(40, 75),),
        blend_colors(accent, accent2, 0.5) + (rng.randint(30, 60),),
        blend_colors(accent, (255, 255, 255), 0.3) + (rng.randint(15, 30),),
    ]
    for color in blob_colors:
        bx = rng.randint(-200, THUMB_W - 200)
        by = rng.randint(-200, THUMB_H - 200)
        br = rng.randint(400, 900)
        bdraw.ellipse([bx - br, by - br, bx + br, by + br], fill=color)
    blobs = blobs.filter(ImageFilter.GaussianBlur(radius=rng.randint(40, 80)))
    base = Image.alpha_composite(base.convert("RGBA"), blobs)

    # ── Layer 2: Noise texture for film grain
    noise = generate_noise_texture(THUMB_W, THUMB_H, intensity=rng.randint(10, 20), seed=seed)
    noise_rgba = noise.convert("RGBA")
    base = Image.blend(base, noise_rgba, alpha=0.12)

    # ── Layer 3: Radial vignette from center-right (behind portrait)
    vignette = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    cx, cy = 1400, THUMB_H // 2
    max_r = int(math.sqrt((THUMB_W - cx) ** 2 + (THUMB_H // 2) ** 2))
    for r in range(max_r, 0, -5):
        alpha = int(200 * (r / max_r) ** 1.5)
        vdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0, min(alpha, 200)))
    vignette = vignette.filter(ImageFilter.GaussianBlur(radius=20))
    base = Image.alpha_composite(base, vignette)

    # ── Layer 4: Light rays from behind portrait area
    rays = draw_light_rays(
        THUMB_W, THUMB_H,
        cx=1350, cy=THUMB_H // 3,
        color=blend_colors(accent, accent2, 0.5),
        num_rays=rng.randint(10, 18),
        spread_deg=rng.randint(55, 85),
    )
    base = Image.alpha_composite(base, rays)

    # ── Layer 5: Bottom gradient darkening
    bottom_dark = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    bd_draw = ImageDraw.Draw(bottom_dark)
    for y in range(THUMB_H):
        alpha = int(100 * (y / THUMB_H) ** 1.8)
        bd_draw.line([(0, y), (THUMB_W, y)], fill=(0, 0, 0, min(alpha, 120)))
    base = Image.alpha_composite(base, bottom_dark)

    return base


# ══════════════════════════════════════════════════════
#  RADIAL VIGNETTE BACKGROUND  (PW style)
# ══════════════════════════════════════════════════════

def build_radial_vignette_bg(accent, accent2, ai_bg_rgba, raw_title=""):
    """
    PW-style background: soft radial vignette with light rays,
    tinted AI background on the right, dark text-safe zone on left.
    Replaces the sharp diagonal split panel.
    """
    canvas = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 255))

    # Use AI background as base (enhanced)
    ai_base = ai_bg_rgba.convert("RGBA")
    # Darken entire AI background slightly
    dark = Image.new("RGBA", ai_base.size, (0, 0, 0, 60))
    ai_base = Image.alpha_composite(ai_base, dark)

    # ── Left side darkener (text zone) — wider and softer than before
    left_dark = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    ld_draw = ImageDraw.Draw(left_dark)
    for x in range(0, 1200, 2):
        alpha = int(190 * (1 - x / 1200) ** 0.6)
        ld_draw.line([(x, 0), (x, THUMB_H)], fill=(0, 0, 0, min(alpha, 190)))
    canvas = Image.alpha_composite(ai_base, left_dark)

    # ── Radial glow behind person (center-right)
    glow = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    cx, cy = 1420, THUMB_H // 2 - 50

    # Outer warm glow rings
    for r in range(600, 100, -40):
        alpha = int(80 * (r / 600) ** 0.8)
        gdraw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=accent + (min(alpha, 80),),
        )
    # Inner bright core
    for r in range(100, 0, -10):
        alpha = int(140 * (1 - r / 100))
        gdraw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=accent2 + (min(alpha, 140),),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=25))
    canvas = Image.alpha_composite(canvas, glow)

    # ── Light rays from behind portrait
    seed = prompt_seed(raw_title or "rays")
    rng = random.Random(seed)
    rays = draw_light_rays(
        THUMB_W, THUMB_H,
        cx=1400, cy=350,
        color=blend_colors(accent, (255, 255, 255), 0.4),
        num_rays=rng.randint(12, 18),
        spread_deg=rng.randint(60, 90),
    )
    canvas = Image.alpha_composite(canvas, rays)

    # ── Vignette edges (all around)
    vignette = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    for r in range(int(max(THUMB_W, THUMB_H) * 0.8), 0, -10):
        alpha = int(160 * (r / (max(THUMB_W, THUMB_H) * 0.8)) ** 0.5)
        vdraw.ellipse(
            [THUMB_W // 2 - r, THUMB_H // 2 - r,
             THUMB_W // 2 + r, THUMB_H // 2 + r],
            fill=(0, 0, 0, 160 - min(alpha, 160)),
        )
    vignette = vignette.filter(ImageFilter.GaussianBlur(radius=15))
    canvas = Image.alpha_composite(canvas, vignette)

    # ── Subtle accent bottom bar (thinner, more refined)
    bar = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(bar)
    bdraw.rectangle([0, THUMB_H - 8, THUMB_W, THUMB_H], fill=accent2 + (180,))
    canvas = Image.alpha_composite(canvas, bar)

    return canvas


def draw_left_gradient_vignette(canvas):
    """Adds a left-to-right fade so text stays readable regardless of background."""
    panel = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(panel)
    width = 1050
    for x in range(width):
        alpha = int(180 * (1 - x / width) ** 0.7)
        pdraw.line([(x, 0), (x, THUMB_H)], fill=(0, 0, 0, alpha))
    return Image.alpha_composite(canvas, panel)


# ══════════════════════════════════════════════════════
#  PERSON BACKING SHAPE  (arch / glow circle)
# ══════════════════════════════════════════════════════

def draw_person_backing(canvas, pos, accent, accent2):
    """
    Draws concentric glowing arches behind the person — like the
    yellow/cream arch seen in trending PW-style thumbnails.
    """
    draw = ImageDraw.Draw(canvas)
    cx = pos[0] + PERSON_SIZE[0] // 2
    cy = pos[1] + PERSON_SIZE[1] - 60  # anchor near feet

    # Light cream/yellow arch layers (outer → inner)
    arch_colors = [
        (255, 245, 200, 40),
        (255, 235, 160, 60),
        (255, 220, 100, 80),
        (255, 210,  60, 100),
    ]
    radii = [480, 400, 320, 240]

    for color, r in zip(arch_colors, radii):
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r // 2],
            fill=color,
        )

    # Inner bright core circle
    r_core = 160
    draw.ellipse(
        [cx - r_core, cy - r_core, cx + r_core, cy + r_core // 2],
        fill=accent2 + (160,),
    )
    return canvas


# ══════════════════════════════════════════════════════
#  TEXT DRAWING  — glow, shadow, highlight
# ══════════════════════════════════════════════════════

def draw_text_shadow(draw, xy, text, font, offset=8, blur_passes=1):
    """Pure-PIL drop shadow (no gaussian on draw layer, approximated with offsets)."""
    x, y = xy
    for dx in range(-offset, offset + 1, 2):
        for dy in range(-offset, offset + 1, 2):
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 120))


def draw_bold_text(draw, xy, text, font, fill, stroke=7):
    x, y = xy
    # Thick black stroke
    for dx, dy in [(-stroke, 0), (stroke, 0), (0, -stroke), (0, stroke),
                   (-stroke, -stroke), (stroke, stroke),
                   (-stroke,  stroke), (stroke, -stroke)]:
        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 255))
    draw.text((x, y), text, font=font, fill=fill)


def draw_highlight_glow(draw, xy, text, font, glow_color):
    """
    Multi-layer glow behind the highlighted word — trending neon/gold effect.
    """
    x, y = xy
    # Outer soft glow
    for r in [18, 12, 7, 4]:
        alpha = max(30, 100 - r * 4)
        for dx in range(-r, r + 1, 3):
            for dy in range(-r, r + 1, 3):
                draw.text((x + dx, y + dy), text, font=font,
                          fill=glow_color + (alpha,))
    # Crisp main text
    draw_bold_text(draw, xy, text, font, glow_color + (255,), stroke=6)


def draw_title_block(draw, title, highlight_word, font,
                     x, y, max_width, highlight_color, normal_color):
    highlight_word = (highlight_word or "").upper()
    words = title.upper().split()
    lines, current = [], []

    for word in words:
        test = " ".join(current + [word])
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(current)
            current = [word]
    if current:
        lines.append(current)

    cy = y
    for line_words in lines:
        cx = x
        for word in line_words:
            bbox = draw.textbbox((0, 0), word, font=font)
            word_w = bbox[2] - bbox[0]
            if word == highlight_word:
                draw_highlight_glow(draw, (cx, cy), word, font, highlight_color)
            else:
                draw_bold_text(draw, (cx, cy), word, font,
                               normal_color + (255,), stroke=7)
            cx += word_w + 18

        line_h = draw.textbbox((0, 0), "Ay", font=font)[3]
        cy += line_h + 16

    return cy


def draw_wrapped_subtitle(draw, text, font, start_x, start_y, max_width, fill_color):
    words = text.split()
    lines, current = [], []
    for word in words:
        test = " ".join(current + [word])
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))

    y = start_y
    for line in lines:
        draw_bold_text(draw, (start_x, y), line, font, fill_color + (255,), stroke=4)
        y += draw.textbbox((0, 0), line, font=font)[3] + 12
    return y


# ══════════════════════════════════════════════════════
#  BADGE & URGENCY STRIP SYSTEM
# ══════════════════════════════════════════════════════

URGENCY_TEMPLATES = [
    # (keywords, label, bar_color, text_color)
    (("one shot",), "ONE SHOT ✦", (255, 215, 0), (0, 0, 0)),
    (("revision", "last minute", "marathon"), "QUICK REVISION ↯", (255, 100, 0), (255, 255, 255)),
    (("miss", "don", "dont", "urgent", "alert"), "⚠ MISS मत करना ⚠", (220, 20, 60), (255, 255, 255)),
    (("record", "recording", "old", "2020", "2021", "2022", "2023", "2024", "2025"),
     "📼 RECORDINGS FROM BATCH", (80, 80, 80), (200, 200, 200)),
    (("free", "live", "youtube"), "🔴 LIVE CLASS", (255, 0, 0), (255, 255, 255)),
    (("pyq", "previous year", "question", "mcq", "quiz"), "PYQ SOLVED", (0, 180, 120), (255, 255, 255)),
    (("mock test", "test series", "practice"), "📝 MOCK TEST", (0, 128, 200), (255, 255, 255)),
    (("crash course", "complete", "full"), "💥 CRASH COURSE", (139, 0, 0), (255, 255, 255)),
    (("strategy", "tips", "tricks", "hack"), "SECRET STRATEGY", (138, 43, 226), (255, 255, 255)),
    (("doubt", "session", "discussion"), "DOUBT SESSION", (0, 168, 107), (255, 255, 255)),
]


def detect_urgency_strip(raw_title):
    """
    Analyze title/subtitle for keywords and return (label, bar_color, text_color).
    Returns None if no urgency keywords matched.
    """
    title_lower = str(raw_title).lower()
    for keywords, label, bar_color, text_color in URGENCY_TEMPLATES:
        if any(kw in title_lower for kw in keywords):
            return label, bar_color, text_color
    return None


def draw_urgency_strip(img, label, bar_color, text_color, font, y_pos=None):
    """
    Draw a full-width or angled urgency strip at the bottom of the image.
    Returns the y-position after the strip.
    """
    draw = ImageDraw.Draw(img)

    strip_h = 72
    y = y_pos if y_pos is not None else THUMB_H - strip_h - 4

    # Shadow for depth
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rectangle([0, y + 4, THUMB_W, y + strip_h + 4], fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=6))
    img = Image.alpha_composite(img, shadow)

    # Main bar
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, y, THUMB_W, y + strip_h], fill=bar_color + (250,))

    # Top thin highlight line
    draw.rectangle([0, y, THUMB_W, y + 3], fill=(255, 255, 255, 120))

    # Centered text
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    text_x = (THUMB_W - text_w) // 2
    text_y = y + (strip_h - (bbox[3] - bbox[1])) // 2 - 4

    draw_bold_text(draw, (text_x, text_y), label, font,
                   text_color + (255,), stroke=5)

    return img, y - 12


def draw_badge(draw, text, font, x, y, bg_color, text_color=(255, 255, 255)):
    """Pill-shaped badge with padding — same style as LIVE / MUST WATCH."""
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0] + 60
    h = bbox[3] - bbox[1] + 28
    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=14,
        fill=bg_color + (255,),
        outline=(255, 255, 255, 180),
        width=3,
    )
    draw_bold_text(draw, (x + 30, y + 14), text, font,
                   text_color + (255,), stroke=2)
    return w, h


# ══════════════════════════════════════════════════════
#  PERSON DROP SHADOW
# ══════════════════════════════════════════════════════

def add_person_shadow(overlay, person_img, pos):
    shadow = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    silhouette = Image.new("RGBA", person_img.size, (0, 0, 0, 210))
    if person_img.mode == "RGBA":
        silhouette.putalpha(person_img.split()[3])
    shadow.paste(silhouette, (pos[0] + 20, pos[1] + 20), silhouette)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=28))
    return Image.alpha_composite(shadow, overlay)


# ══════════════════════════════════════════════════════
#  LLM / GROQ
# ══════════════════════════════════════════════════════

def _pick_highlight_word(title):
    stop = {"the", "a", "an", "in", "on", "for", "to", "of",
            "and", "or", "is", "how", "what", "vs", "by"}
    words = re.findall(r"[A-Za-z0-9]+", str(title))
    for w in words:
        if len(w) >= 4 and w.lower() not in stop:
            return w.upper()
    return words[0].upper() if words else "NOW"


def merge_llm_response(raw_title, parsed):
    default = {
        "main_title":      str(raw_title).strip(),
        "subtitle":        "Watch Before You Miss It",
        "badge":           "MUST WATCH",
        "highlight_word":  _pick_highlight_word(raw_title),
        "image_prompt": (
            f"Cinematic dramatic scene related to '{raw_title}', "
            "vibrant neon or fire or storm lighting, extreme depth of field, "
            "highly detailed background objects, no people, no text, 8K, "
            "movie poster quality, Awwwards editorial style"
        ),
        "accent_color":    "#EF4444",
        "highlight_color": "#FACC15",
        "subtitle_color":  "#F59E0B",
        "theme":           "education",
        "layout_style":    "split",
    }
    if not isinstance(parsed, dict):
        return default
    out = default.copy()
    for key in LLM_KEYS:
        val = parsed.get(key)
        if val is not None and str(val).strip():
            out[key] = str(val).strip()
    if not out.get("highlight_word"):
        out["highlight_word"] = _pick_highlight_word(out.get("main_title", raw_title))
    return out


def ask_context_llm_agent(raw_title):
    print("🧠 Groq: generating thumbnail layout...", flush=True)
    default_response = merge_llm_response(raw_title, None)

    system_instruction = (
        "You design VIRAL Indian educational YouTube thumbnails (MrBeast / PW / StudyIQ style). "
        "Output ONLY valid JSON — no markdown, no explanation:\n"
        "- main_title: 3-5 POWER words, ALL CAPS emotion (shocking/urgent/clear). Title Case ok.\n"
        "- subtitle: urgency or key benefit, max 7 words\n"
        "- badge: 1-2 words (LIVE, NEW, SECRET, MUST WATCH, BREAKING, etc.)\n"
        "- highlight_word: ONE word from main_title to glow gold (most important word)\n"
        "- image_prompt: SPECIFIC cinematic background scene. Include lighting type "
        "(neon, fire, storm, bokeh), specific objects related to the topic, color mood. "
        "NO people, NO faces, NO text, NO watermark. Movie poster quality.\n"
        "- accent_color: hex — pick a BOLD color matching topic energy "
        "(red for war/urgent, blue for tech, gold for achievement, etc.)\n"
        "- highlight_color: hex — bright gold #FACC15 or electric blue #38BDF8 or lime #84CC16\n"
        "- subtitle_color: hex — slightly muted version of highlight\n"
        "- theme: one word (finance, devops, history, science, motivation, exam, etc.)\n"
        "- layout_style: always 'split'\n"
        "Think: if this thumbnail appeared in YouTube search, it must FORCE a click."
    )

    try:
        if not groq_client or not GROQ_API_KEY:
            print("⚠ GROQ_API_KEY not set — using built-in defaults.", flush=True)
            return default_response

        chat_completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user",   "content": f"Video title: {raw_title}"},
            ],
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            temperature=0.60,
            response_format={"type": "json_object"},
        )

        if chat_completion and chat_completion.choices:
            cleaned = chat_completion.choices[0].message.content.strip()
            return merge_llm_response(raw_title, json.loads(cleaned))
        return default_response

    except Exception as e:
        print(f"⚠ Groq failed: {e}. Using defaults.", flush=True)
        return default_response


# ══════════════════════════════════════════════════════
#  POLLINATIONS
# ══════════════════════════════════════════════════════

def fetch_pollinations_image(prompt, seed):
    # ── Bake negative keywords directly into the prompt (the API reads them from text)
    style_suffix = (
        ", ultra detailed, cinematic lighting, dramatic contrast, "
        "8K UHD, movie poster quality, award-winning photography, "
        "no people, no faces, no text, no watermark, no UI elements, "
        "no blurry, no dull colors, no flat lighting"
    )
    full_prompt = (prompt + style_suffix).strip()[:950]

    # ── Correct endpoint: /prompt/<encoded-text>  (NOT /p/)
    encoded = requests.utils.quote(full_prompt, safe="")
    base_url = f"https://image.pollinations.ai/prompt/{encoded}"

    # ── Only valid query params for this endpoint
    params = {
        "width":   THUMB_W,
        "height":  THUMB_H,
        "model":   POLLINATIONS_MODEL,
        "seed":    seed,
        "enhance": "true",
        "nologo":  "true",
        "private": "true",
        "nofeed":  "true",
    }

    print(f"   → GET {base_url[:80]}... seed={seed}", flush=True)
    response = http_session.get(base_url, params=params, timeout=POLLINATIONS_TIMEOUT)
    print(f"   ← HTTP {response.status_code}  size={len(response.content)} bytes", flush=True)

    if response.status_code == 200 and len(response.content) > 5000:
        return Image.open(io.BytesIO(response.content))

    # ── 402 = payment/queue full — skip retries immediately
    if response.status_code == 402:
        snippet = response.text[:200].replace("\n", " ")
        print(f"   ✗ 402 Queue Full — skipping retries. {snippet}", flush=True)
        return None

    # Log other errors
    if response.status_code != 200:
        snippet = response.text[:200].replace("\n", " ")
        print(f"   ✗ Error body: {snippet}", flush=True)
    return None


def query_photorealistic_engine(prompt, seed_base):
    global _last_pollinations_call

    # ── Free-tier cooldown: only 1 request every ~120s (safer than 15s)
    now = time.time()
    elapsed = now - _last_pollinations_call
    if elapsed < _POLLINATIONS_COOLDOWN:
        wait = _POLLINATIONS_COOLDOWN - elapsed
        print(f"   ⏳ Pollinations on cooldown ({int(wait)}s left) — using fallback.", flush=True)
        return None

    print(f"🎨 Pollinations ({POLLINATIONS_MODEL})...", flush=True)
    for attempt in range(3):
        seed = (seed_base + attempt * 9973) % 2147483646
        try:
            img = fetch_pollinations_image(prompt, seed)
            if img:
                _last_pollinations_call = time.time()
                print(f"✨ Background ready (attempt {attempt + 1}).", flush=True)
                return img
        except Exception as e:
            print(f"⚠ Attempt {attempt + 1} exception: {type(e).__name__}: {e}", flush=True)

        # Don't waste time retrying if we got 402 (fetch already logged + returned None)
        # Only retry on network/timeout errors
        if attempt < 2:
            print(f"   ⏳ Waiting 16s before retry...", flush=True)
            time.sleep(16)

    print("❌ Pollinations failed — gradient fallback.", flush=True)
    return None


def load_reference_palette():
    if not os.path.isdir(REFERENCE_DIR):
        return None
    for name in sorted(os.listdir(REFERENCE_DIR)):
        if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            try:
                img = Image.open(os.path.join(REFERENCE_DIR, name)).convert("RGB")
                img = img.resize((120, 68))
                colors = img.getcolors(maxcolors=256 * 256)
                if not colors:
                    continue
                colors.sort(key=lambda c: c[0], reverse=True)
                return colors[0][1], colors[min(3, len(colors) - 1)][1]
            except Exception:
                continue
    return None


def build_background(layout, raw_title, drive_service, custom_bg_url):
    """
    Build background with keyword-enhanced colors.
    Keyword color map enhances/replaces generic Groq defaults for consistency.
    """
    # Get keyword-based theme colors
    kw_accent, kw_highlight, kw_subtitle, kw_theme = keyword_color_map(raw_title)

    # Parse Groq colors, falling back to keyword colors if Groq returns defaults
    groq_accent  = parse_hex_color(layout.get("accent_color"),    None)
    groq_accent2 = parse_hex_color(layout.get("highlight_color"), None)
    groq_line    = parse_hex_color(layout.get("subtitle_color"),  None)

    # If Groq returned colors OR keyword gives more specific theme, blend toward keyword
    if groq_accent:
        accent = groq_accent
    else:
        accent = kw_accent
        print(f"   🎨 Keyword theme '{kw_theme}' → accent {rgb_to_hex(kw_accent)}", flush=True)

    if groq_accent2:
        accent2 = groq_accent2
    else:
        accent2 = kw_highlight
        print(f"   🎨 Keyword theme '{kw_theme}' → highlight {rgb_to_hex(kw_highlight)}", flush=True)

    if groq_line:
        accent_line = groq_line
    else:
        accent_line = kw_subtitle

    # Update layout dict with the resolved colors (so rest of pipeline uses them)
    layout["accent_color"]     = rgb_to_hex(accent)
    layout["highlight_color"]  = rgb_to_hex(accent2)
    layout["subtitle_color"]   = rgb_to_hex(accent_line)
    if kw_theme:
        layout["theme"] = kw_theme

    seed = prompt_seed(layout.get("main_title", "") + layout.get("theme", ""))

    # Custom background from sheet column D
    if custom_bg_url and extract_drive_id(custom_bg_url):
        print("🖼 Using custom background from sheet column D.", flush=True)
        file_id = extract_drive_id(custom_bg_url)
        if file_id:
            raw = download_drive_file(drive_service, file_id)
            bg = enhance_background(Image.open(io.BytesIO(raw)), accent=accent)
            if bg:
                return bg

    img = query_photorealistic_engine(layout["image_prompt"], seed)
    if img:
        return enhance_background(img, accent=accent)

    ref = load_reference_palette()
    if ref:
        accent, accent2 = ref[0], ref[1]
    return make_chaotic_gradient_fallback(accent, accent2, layout.get("main_title", ""))


# ══════════════════════════════════════════════════════
#  MAIN COMPOSE  — the heart of the visual upgrade
# ══════════════════════════════════════════════════════

def compose_modern_thumbnail(data, ai_bg, creator_no_bg, raw_title):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    ai_bg          = ai_bg.convert("RGBA")
    creator_no_bg  = creator_no_bg.convert("RGBA")

    accent      = parse_hex_color(data.get("accent_color"),    (239, 68, 68))
    highlight   = parse_hex_color(data.get("highlight_color"), (250, 204, 21))
    accent_line = parse_hex_color(data.get("subtitle_color"),  (245, 158, 11))

    # ── 1. Build PW-style radial vignette background
    canvas = build_radial_vignette_bg(accent, highlight, ai_bg, raw_title=raw_title)

    # ── 2. Left-side reading vignette (extra text safety)
    canvas = draw_left_gradient_vignette(canvas)

    # ── 3. Arch backing behind person
    canvas = draw_person_backing(canvas, PERSON_POS, accent, highlight)

    # ── 4. Person drop shadow
    canvas = add_person_shadow(canvas, creator_no_bg, PERSON_POS)

    # ── 5. Paste person cutout
    canvas.paste(creator_no_bg, PERSON_POS, creator_no_bg)

    # ── 6. Text layer
    text_layer = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_layer)

    font_title, font_sub, font_badge = get_fonts()

    # Badge (top-left pill)
    badge_text = data.get("badge", "NEW").upper()
    draw_badge(draw, badge_text, font_badge, x=70, y=110,
               bg_color=accent, text_color=(255, 255, 255))

    # Title block — starts lower to give badge breathing room
    next_y = draw_title_block(
        draw,
        data.get("main_title", raw_title),
        data.get("highlight_word", ""),
        font_title,
        x=70, y=230,
        max_width=950,
        highlight_color=highlight,
        normal_color=(255, 255, 255),
    )

    # Accent divider line (thicker, shorter — more modern)
    line_y = next_y + 22
    draw.rounded_rectangle(
        [70, line_y, 680, line_y + 8],
        radius=4,
        fill=accent_line + (255,),
    )

    # Subtitle
    end_y = draw_wrapped_subtitle(
        draw,
        data.get("subtitle", ""),
        font_sub,
        start_x=70, start_y=line_y + 32,
        max_width=940,
        fill_color=(255, 255, 255),
    )

    # Brand tagline (smaller, grey)
    if BRAND_TAGLINE.strip():
        draw.text(
            (70, end_y + 18),
            BRAND_TAGLINE,
            fill=(190, 190, 190, 200),
            font=font_badge,
        )

    # Left-edge accent bar (thin vertical strip)
    draw.rectangle([0, 0, 10, THUMB_H], fill=accent + (230,))

    canvas = Image.alpha_composite(canvas, text_layer)

    # ── 7. Urgency strip (if title keywords match)
    urgency = detect_urgency_strip(raw_title + " " + data.get("subtitle", ""))
    if urgency:
        label, bar_color, text_color = urgency
        print(f"   🚨 Urgency strip triggered: {label}", flush=True)
        # To draw strip on top of final, convert to RGBA, composite, then back
        canvas_rgba = canvas.convert("RGBA")
        canvas_rgba, _ = draw_urgency_strip(canvas_rgba, label, bar_color, text_color, font_badge)
        canvas = canvas_rgba

    # ── 8. Final colour grading
    final = canvas.convert("RGB")
    final = ImageEnhance.Contrast(final).enhance(1.10)
    final = ImageEnhance.Sharpness(final).enhance(1.10)
    final = ImageOps.autocontrast(final, cutoff=1)

    # ── 9. Save
    clean_title = (
        "".join(c if c.isalnum() or c.isspace() else "" for c in raw_title)
        .strip().replace(" ", "_")[:80]
    )
    if not clean_title:
        clean_title = f"Render_{int(time.time())}"

    file_path = os.path.join(OUTPUT_DIR, f"{clean_title}.jpg")
    final.save(file_path, format="JPEG", quality=97, subsampling=0, optimize=True)
    print(f"🚀 Saved thumbnail: {file_path}\n", flush=True)
    return file_path


# ══════════════════════════════════════════════════════
#  GOOGLE DRIVE / SHEET HELPERS
# ══════════════════════════════════════════════════════

def download_drive_file(drive_service, file_id):
    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()


def set_row_status(sheet_service, row_num, status):
    sheet_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!C{row_num}",
        valueInputOption="USER_ENTERED",
        body={"values": [[status]]},
    ).execute()


def row_is_skipped(row):
    if len(row) < 2 or not str(row[0]).strip():
        return True
    if len(row) >= 3:
        status = str(row[2]).strip().upper()
        if status in ("DONE", "PROCESSING"):
            return True
    return False


def _categorize_error(e):
    """Return a user-friendly error tag from an exception."""
    msg = str(e).lower()
    if "timeout" in msg or "timed out" in msg:
        return "TIMEOUT"
    if "quota" in msg or "rate limit" in msg:
        return "RATE_LIMIT"
    if "not found" in msg or "404" in msg:
        return "NOT_FOUND"
    if "permission" in msg or "403" in msg:
        return "PERMISSION"
    return "ERROR"


def process_single_row(sheet_service, drive_service, row, row_num):
    raw_title     = row[0].strip()
    drive_url     = row[1].strip()
    custom_bg_url = row[3].strip() if len(row) > 3 else ""

    print(f"⚡ Row {row_num}: {raw_title}", flush=True)
    set_row_status(sheet_service, row_num, "PROCESSING")

    file_id = extract_drive_id(drive_url)
    if not file_id:
        raise ValueError(f"Could not parse Drive URL: {drive_url}")

    try:
        raw_bytes = download_drive_file(drive_service, file_id)
    except Exception as e:
        tag = _categorize_error(e)
        print(f"   📡 Drive download {tag}: {e}", flush=True)
        raise RuntimeError(f"Drive {tag}: {e}")

    try:
        layout = ask_context_llm_agent(raw_title)
    except Exception as e:
        tag = _categorize_error(e)
        print(f"   🧠 LLM {tag}: {e}", flush=True)
        raise RuntimeError(f"LLM {tag}: {e}")

    accent = parse_hex_color(layout.get("highlight_color"), (250, 204, 21))

    # Pass accent color to rim light so the glow matches the theme
    try:
        creator = prepare_portrait(raw_bytes, rim_color=accent)
    except Exception as e:
        print(f"   ✂️ Portrait cutout failed: {e}", flush=True)
        creator = Image.new("RGBA", PERSON_SIZE, (0, 0, 0, 0))

    try:
        bg = build_background(layout, raw_title, drive_service, custom_bg_url)
    except Exception as e:
        print(f"   🎨 Background build failed: {e}", flush=True)
        # Fallback to chaotic gradient if build_background itself crashes
        kw_accent, kw_highlight, _, _ = keyword_color_map(raw_title)
        bg = make_chaotic_gradient_fallback(kw_accent, kw_highlight, raw_title)

    compose_modern_thumbnail(layout, bg, creator, raw_title)
    set_row_status(sheet_service, row_num, "DONE")


# ══════════════════════════════════════════════════════
#  ORCHESTRATOR
# ══════════════════════════════════════════════════════

def start_orchestrator():
    print("🛰 Thumbnail agent: scanning spreadsheet...", flush=True)
    sheet_service, drive_service = get_google_services()
    result = (
        sheet_service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_NAME}!A2:D500")
        .execute()
    )
    rows = result.get("values", [])
    if not rows:
        print("ℹ No rows in sheet.", flush=True)
        return

    for index, row in enumerate(rows):
        if row_is_skipped(row):
            continue
        row_num = index + 2
        try:
            process_single_row(sheet_service, drive_service, row, row_num)
        except Exception as e:
            print(f"❌ Row {row_num} failed: {e}", flush=True)
            try:
                set_row_status(sheet_service, row_num, "ERROR")
            except Exception:
                pass


def main():
    if not os.path.exists(GOOGLE_CREDS_FILE):
        print(f"❌ Missing {GOOGLE_CREDS_FILE}", flush=True)
        raise SystemExit(1)
    if not GROQ_API_KEY:
        print("⚠ GROQ_API_KEY not set — generic titles/colors.", flush=True)
    if USE_REMBG and not rembg_remove:
        print("⚠ rembg not installed — using basic cutout.", flush=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(REFERENCE_DIR, exist_ok=True)
    print(f"📁 Output: {OUTPUT_DIR}", flush=True)

    while True:
        try:
            start_orchestrator()
        except Exception as e:
            print(f"❌ Orchestrator error: {e}", flush=True)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
