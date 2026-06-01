from __future__ import annotations

import csv
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from io import BytesIO, StringIO
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    Response,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

try:
    import qrcode
except Exception:  # pragma: no cover
    qrcode = None


def load_env_file() -> None:
    """Load simple KEY=VALUE pairs from a local .env file, if it exists."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-local-dev-key")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "marketing.db"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change_me")
BRAND_NAME = os.getenv("BRAND_NAME", "Offroad Bumpis")
BOOKING_URL = os.getenv("BOOKING_URL", "https://offroad-bumpis-booking.onrender.com/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

CHANNELS = [
    "Facebook-grupp",
    "Instagram",
    "Facebook-sida",
    "Google Business Profile",
    "Reddit",
    "Mail",
    "Flyer",
    "SEO/blogg",
]

AUDIENCES = [
    "Föräldrar / barnkalas",
    "Skola / fritids",
    "Förening / lag",
    "Företag / kickoff",
    "Café / park / samarbeten",
    "Blandat lokalt",
]

PRODUCTS = [
    "Barnbollar",
    "Vuxenbollar",
    "Barnbollar + vuxenbollar",
    "Turneringskit",
    "Bumperballs med leverans",
]


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Optional[BaseException]) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            audience TEXT NOT NULL,
            product TEXT NOT NULL,
            area TEXT NOT NULL,
            offer TEXT NOT NULL,
            target_url TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            notes TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            channel TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            result TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            created_at TEXT NOT NULL,
            task_date TEXT NOT NULL,
            channel TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'todo',
            result TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            lead_type TEXT NOT NULL,
            municipality TEXT,
            contact TEXT,
            source TEXT,
            campaign_id INTEGER,
            status TEXT NOT NULL DEFAULT 'ny',
            next_step TEXT,
            note TEXT,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            campaign_id INTEGER,
            code TEXT NOT NULL,
            ref TEXT,
            user_agent TEXT,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS posting_places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'Facebook-grupp',
            url TEXT,
            area TEXT,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    ensure_column(db, "posts", "version", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(db, "tasks", "version", "INTEGER NOT NULL DEFAULT 1")
    db.commit()


with app.app_context():
    init_db()


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today_iso() -> str:
    return date.today().isoformat()


def clean_code(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", (value or "").upper())
    return cleaned[:18] or "KAMPANJ"


def unique_code(name: str) -> str:
    db = get_db()
    base = clean_code(name)[:10]
    for _ in range(20):
        code = f"{base}{secrets.token_hex(2).upper()}"
        exists = db.execute("SELECT id FROM campaigns WHERE code = ?", (code,)).fetchone()
        if not exists:
            return code
    return f"KAMPANJ{secrets.token_hex(4).upper()}"


def public_url_for(endpoint: str, **values) -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{url_for(endpoint, **values)}"
    return url_for(endpoint, _external=True, **values)


def tracking_url(campaign: sqlite3.Row | dict, ref: str) -> str:
    return public_url_for("track_click", code=campaign["code"], ref=ref)


def get_campaign(campaign_id: int) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()


def campaign_options() -> dict:
    return {"channels": CHANNELS, "audiences": AUDIENCES, "products": PRODUCTS, "booking_url": BOOKING_URL}


def next_content_version(campaign_id: int) -> int:
    row = get_db().execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM posts WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()
    return int(row["v"]) + 1


def campaign_angle(campaign: sqlite3.Row | dict) -> dict[str, str]:
    audience = (campaign["audience"] or "").lower()

    if "skola" in audience or "fritids" in audience:
        return {
            "who": "skolor, fritids och klassföräldrar",
            "hook": "Planera en aktivitet där många barn kan vara med utan att du behöver uppfinna OS 2.0.",
            "benefits": "- enkel aktivitet för många barn\n- funkar på gräs, konstgräs eller i idrottshall\n- batteridriven pump ingår\n- tydligt upplägg och möjlighet till turneringskit",
            "cta": "Vill du ha en enkel aktivitet till skolavslutning, fritidsdag eller klassaktivitet?",
            "mail_subject": "Bumperballs till skola, fritids eller avslutning",
        }

    if "förening" in audience or "lag" in audience:
        return {
            "who": "lag, föreningar och ungdomsgrupper",
            "hook": "Perfekt till avslutning, cupdag eller en träning där alla faktiskt skrattar.",
            "benefits": "- roligt för lagaktivitet och avslutning\n- kan köras som matcher eller fri lek\n- funkar på gräs, konstgräs eller i hall\n- leverans kan bokas inom Skåne",
            "cta": "Vill du göra nästa avslutning lite roligare än korv + diplom?",
            "mail_subject": "Bumperballs till förening eller lagavslutning",
        }

    if "företag" in audience or "kickoff" in audience:
        return {
            "who": "företag, arbetslag och event",
            "hook": "Kickoff utan PowerPoint-döden. Bumperballs är enkelt, fysiskt och svårt att inte skratta åt.",
            "benefits": "- passar AW, kickoff och företagsevent\n- vuxenbollar finns/kan bokas enligt tillgänglighet\n- funkar ute eller i hall\n- leverans kan bokas inom Skåne",
            "cta": "Vill du boka en aktivitet som inte känns som ännu en obligatorisk mingelövning?",
            "mail_subject": "Bumperballs till kickoff eller företagsevent",
        }

    if "café" in audience or "park" in audience:
        return {
            "who": "caféer, parker och lokala samarbeten",
            "hook": "Bumperballs nära café/park kan ge både aktivitet och fler fikagäster. Två flugor, en uppblåsbar boll.",
            "benefits": "- passar parker och öppna ytor nära café\n- flyers/QR kan lämnas där folk redan rör sig\n- batteridriven pump, inget eluttag behövs\n- lokalt upplägg i Skåne",
            "cta": "Vill du tipsa om en plats eller samarbeta lokalt?",
            "mail_subject": "Lokalt samarbete med bumperballs",
        }

    return {
        "who": "föräldrar, barnkalas och lokala grupper",
        "hook": "Barnkalas som inte slutar med att alla sitter med varsin skärm efter 12 minuter.",
        "benefits": "- roligt till barnkalas, skolavslutning och kompisgäng\n- batteridriven pump ingår\n- funkar på gräs, konstgräs eller i hall\n- boka nu och betala senare enligt villkor",
        "cta": "Vill du boka ett datum eller bara ha något kul att se fram emot?",
        "mail_subject": "Bumperballs till barnkalas eller aktivitet",
    }


def generate_content_for_campaign(campaign: sqlite3.Row) -> list[dict[str, str]]:
    c = dict(campaign)
    area = c["area"].strip() or "Skåne"
    offer = c["offer"].strip() or "Boka datum nu och betala senast 3 dagar före utlämning."
    product = c["product"].strip() or "Bumperballs"
    audience = c["audience"].strip() or "lokala kunder"
    angle = campaign_angle(c)

    fb_link = tracking_url(c, "facebook-grupp")
    insta_link = tracking_url(c, "instagram")
    gbp_link = tracking_url(c, "google-business")
    reddit_link = tracking_url(c, "reddit")
    mail_link = tracking_url(c, "mail")
    flyer_link = tracking_url(c, "flyer")
    seo_link = tracking_url(c, "seo")
    page_ref = area.split(",")[0].strip() or "Skåne"

    return [
        {
            "channel": "Facebook-grupp",
            "title": f"FB-grupp: {area}",
            "body": f"""Tips till er som vill göra något roligt i {area} 👇

Jag hyr ut {product.lower()} via Offroad Bumpis. {angle['hook']}

Passar för {angle['who']}. Det är stora uppblåsbara bollar som man springer runt i, krockar lite lagom och ramlar utan att det gör ont.

{angle['benefits']}

{offer}

Boka här:
{fb_link}

#offroadbumpis #bumperballs #skåne #barnkalas #aktivitet""",
        },
        {
            "channel": "Instagram",
            "title": f"Instagram: {campaign['name']}",
            "body": f"""Bumperballs i {area} ⚽️💥

{angle['hook']}

✅ {product}
✅ För {audience.lower()}
✅ Batteridriven pump
✅ Gräs, konstgräs eller hall
✅ {offer}

Boka här:
{insta_link}

#offroadbumpis #bumperballs #skåne #malmö #lund #trelleborg #helsingborg #barnkalas #aktivitetförbarn""",
        },
        {
            "channel": "Facebook-sida",
            "title": "Facebook-sida: kampanjpost",
            "body": f"""Ny kampanj: {campaign['name']}

Offroad Bumpis hyr ut {product.lower()} i {area}. Passar för {audience.lower()}.

{angle['cta']}

{offer}

Boka här:
{tracking_url(c, 'facebook-sida')}""",
        },
        {
            "channel": "Google Business Profile",
            "title": "Google Business: lokalt inlägg",
            "body": f"""Hyr {product.lower()} i {area}

Offroad Bumpis erbjuder bumperballs för kalas, skolor, fritids, föreningar och event. Enkel bokning, tydliga priser och möjlighet till leverans inom Skåne.

{offer}

Boka här:
{gbp_link}""",
        },
        {
            "channel": "Reddit",
            "title": f"Reddit-fråga: platser i {area}",
            "body": f"""Hej! Jag hyr ut bumperballs i Skåne och uppdaterar min lista med bra platser där man kan spela.

Har ni tips på bra gräsytor, konstgräsplaner eller hallar i {area} som passar för bumperballs?

Det bästa är platser utan stenar, glas, måsskit och andra små katastrofer. Gärna nära toa/café om det finns.

Jag tar gärna emot tips och lägger in dem på sidan sen. Min sida är Offroad Bumpis om någon vill kika:
{reddit_link}""",
        },
        {
            "channel": "Mail",
            "title": f"Mail: {audience}",
            "body": f"""Ämne: {angle['mail_subject']} i {page_ref}

Hej!

Jag heter Erica och driver Offroad Bumpis i Skåne.

Jag hyr ut {product.lower()} till bland annat skolor, fritids, föreningar, kalas och gruppaktiviteter.

Kort upplägg:
{angle['benefits']}
- {offer}

Här finns bokning och mer info:
{mail_link}

Vänliga hälsningar
Erica
Offroad Bumpis""",
        },
        {
            "channel": "Flyer",
            "title": f"Flyertext: {area}",
            "body": f"""BUMPERBALLS I {area.upper()}

Hyr stora uppblåsbara bollar till kalas, skola, fritids, förening eller event.

✅ Roligt, enkelt och annorlunda
✅ Barnbollar och vuxenbollar
✅ Batteridriven pump
✅ Leverans inom Skåne

{offer}

Boka här:
{flyer_link}

Tips: sätt QR-koden från kampanjsidan på flyern.""",
        },
        {
            "channel": "SEO/blogg",
            "title": f"SEO-idé: {area}",
            "body": f"""Sidtitel:
Hyr bumperballs i {page_ref} | Offroad Bumpis

Meta description:
Hyr bumperballs i {page_ref} till barnkalas, skola, fritids, förening eller event. Enkel bokning, tydliga priser och leverans inom Skåne.

Rubrik:
Bumperballs i {page_ref}

Textstart:
Vill du hyra bumperballs i {page_ref}? Offroad Bumpis hyr ut {product.lower()} till {audience.lower()}. Det funkar bäst på gräs, konstgräs eller i idrottshall.

Sektioner att lägga till:
- Vad ingår?
- Var kan man spela i {page_ref}?
- Pris och leverans
- Boka datum

CTA:
Boka här: {seo_link}""",
        },
    ]


def generate_tasks_for_campaign(campaign: sqlite3.Row) -> list[dict[str, str]]:
    start = date.today()
    area = campaign["area"] or "Skåne"
    audience = (campaign["audience"] or "").lower()

    tasks = [
        {"task_date": (start + timedelta(days=0)).isoformat(), "channel": "Facebook-grupp", "title": f"Dela kampanjen i 2 relevanta grupper för {area}."},
        {"task_date": (start + timedelta(days=1)).isoformat(), "channel": "Instagram", "title": "Lägg upp bild/reel + använd Instagramtexten."},
        {"task_date": (start + timedelta(days=2)).isoformat(), "channel": "Google Business Profile", "title": "Publicera lokalt Google-inlägg."},
        {"task_date": (start + timedelta(days=3)).isoformat(), "channel": "Mail", "title": "Skicka mailet till 5 relevanta kontakter."},
        {"task_date": (start + timedelta(days=4)).isoformat(), "channel": "Flyer", "title": "Lägg flyers på 1–2 platser nära park/café/hall."},
        {"task_date": (start + timedelta(days=5)).isoformat(), "channel": "Lead", "title": "Följ upp alla som svarat, gillat eller frågat."},
        {"task_date": (start + timedelta(days=6)).isoformat(), "channel": "Analys", "title": "Kolla vad som gav klick/bokning. Skrota resten utan sentimentalitet."},
    ]

    if "skola" in audience or "fritids" in audience:
        tasks.insert(1, {"task_date": start.isoformat(), "channel": "Mail", "title": "Maila 5 skolor/fritids eller klassföräldrar i området."})
    elif "företag" in audience or "kickoff" in audience:
        tasks.insert(1, {"task_date": start.isoformat(), "channel": "Mail", "title": "Maila 5 lokala företag/eventansvariga."})
    elif "förening" in audience or "lag" in audience:
        tasks.insert(1, {"task_date": start.isoformat(), "channel": "Mail", "title": "Maila 5 föreningar eller lagledare."})

    return tasks


@app.route("/")
def index():
    if session.get("admin"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("dashboard"))
        return render_template("marketing_login.html", error="Fel lösenord.")
    return render_template("marketing_login.html")


@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin", methods=["GET", "POST"])
@admin_required
def dashboard():
    db = get_db()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        audience = request.form.get("audience", "Blandat lokalt").strip()
        product = request.form.get("product", "Bumperballs med leverans").strip()
        area = request.form.get("area", "Skåne").strip()
        offer = request.form.get("offer", "Boka utan kostnad. Betala senast 3 dagar före utlämning.").strip()
        target_url = request.form.get("target_url", BOOKING_URL).strip() or BOOKING_URL
        code = clean_code(request.form.get("code", "")) if request.form.get("code") else unique_code(name)
        notes = request.form.get("notes", "").strip()

        if not name:
            return redirect(url_for("dashboard"))

        exists = db.execute("SELECT id FROM campaigns WHERE code = ?", (code,)).fetchone()
        if exists:
            code = unique_code(name)

        cur = db.execute(
            """
            INSERT INTO campaigns (created_at, name, audience, product, area, offer, target_url, code, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (now_iso(), name, audience, product, area, offer, target_url, code, notes),
        )
        db.commit()
        return redirect(url_for("campaign_view", campaign_id=cur.lastrowid))

    stats = {
        "active_campaigns": db.execute("SELECT COUNT(*) AS c FROM campaigns WHERE status = 'active'").fetchone()["c"],
        "todo_tasks": db.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = 'todo'").fetchone()["c"],
        "open_leads": db.execute("SELECT COUNT(*) AS c FROM leads WHERE status NOT IN ('bokad', 'död')").fetchone()["c"],
        "clicks_14": db.execute("SELECT COUNT(*) AS c FROM clicks WHERE created_at >= ?", ((datetime.now() - timedelta(days=14)).isoformat(sep=" "),)).fetchone()["c"],
    }
    campaigns = db.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 20").fetchall()
    tasks = db.execute(
        """
        SELECT t.*, c.name AS campaign_name
        FROM tasks t
        LEFT JOIN campaigns c ON c.id = t.campaign_id
        WHERE t.status = 'todo'
        ORDER BY t.task_date ASC, t.id ASC
        LIMIT 12
        """
    ).fetchall()
    focus_tasks = db.execute(
        """
        SELECT t.*, c.name AS campaign_name
        FROM tasks t
        LEFT JOIN campaigns c ON c.id = t.campaign_id
        WHERE t.status = 'todo'
          AND t.task_date <= ?
        ORDER BY t.task_date ASC, t.id ASC
        LIMIT 5
        """,
        (today_iso(),),
    ).fetchall()
    leads = db.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 8").fetchall()
    clicks = db.execute(
        """
        SELECT clicks.*, campaigns.name AS campaign_name
        FROM clicks
        LEFT JOIN campaigns ON campaigns.id = clicks.campaign_id
        ORDER BY clicks.id DESC
        LIMIT 12
        """
    ).fetchall()
    places = db.execute("SELECT * FROM posting_places WHERE status = 'active' ORDER BY id DESC LIMIT 8").fetchall()

    return render_template(
        "marketing_dashboard.html",
        stats=stats,
        campaigns=campaigns,
        tasks=tasks,
        focus_tasks=focus_tasks,
        leads=leads,
        clicks=clicks,
        places=places,
        today=today_iso(),
        **campaign_options(),
    )


@app.route("/admin/campaign/<int:campaign_id>")
@admin_required
def campaign_view(campaign_id: int):
    db = get_db()
    campaign = get_campaign(campaign_id)
    if not campaign:
        return redirect(url_for("dashboard"))

    posts = db.execute("SELECT * FROM posts WHERE campaign_id = ? ORDER BY version DESC, id DESC", (campaign_id,)).fetchall()
    tasks = db.execute("SELECT * FROM tasks WHERE campaign_id = ? ORDER BY version DESC, task_date ASC, id ASC", (campaign_id,)).fetchall()
    leads = db.execute("SELECT * FROM leads WHERE campaign_id = ? ORDER BY id DESC", (campaign_id,)).fetchall()
    clicks = db.execute(
        "SELECT ref, COUNT(*) AS total FROM clicks WHERE campaign_id = ? GROUP BY ref ORDER BY total DESC",
        (campaign_id,),
    ).fetchall()
    version_row = db.execute(
        "SELECT COALESCE(MAX(version), 0) AS latest_version, COUNT(*) AS post_count FROM posts WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()

    tracking_links = [
        {"label": "Facebook-grupp", "url": tracking_url(campaign, "facebook-grupp")},
        {"label": "Instagram", "url": tracking_url(campaign, "instagram")},
        {"label": "Google Business", "url": tracking_url(campaign, "google-business")},
        {"label": "Mail", "url": tracking_url(campaign, "mail")},
        {"label": "Flyer", "url": tracking_url(campaign, "flyer")},
    ]
    posting_places = db.execute("SELECT * FROM posting_places WHERE status = 'active' ORDER BY channel, name").fetchall()

    return render_template(
        "marketing_campaign.html",
        campaign=campaign,
        posts=posts,
        tasks=tasks,
        leads=leads,
        clicks=clicks,
        tracking_links=tracking_links,
        latest_version=int(version_row["latest_version"]),
        post_count=int(version_row["post_count"]),
        posting_places=posting_places,
        **campaign_options(),
    )


@app.post("/admin/campaign/<int:campaign_id>/update")
@admin_required
def campaign_update(campaign_id: int):
    db = get_db()
    status = request.form.get("status", "active")
    notes = request.form.get("notes", "")
    offer = request.form.get("offer", "")
    target_url = request.form.get("target_url", BOOKING_URL).strip() or BOOKING_URL
    db.execute(
        "UPDATE campaigns SET status = ?, notes = ?, offer = ?, target_url = ? WHERE id = ?",
        (status, notes, offer, target_url, campaign_id),
    )
    db.commit()
    return redirect(url_for("campaign_view", campaign_id=campaign_id))


@app.post("/admin/campaign/<int:campaign_id>/generate")
@admin_required
def campaign_generate(campaign_id: int):
    db = get_db()
    campaign = get_campaign(campaign_id)
    if not campaign:
        return redirect(url_for("dashboard"))

    mode = request.form.get("mode", "replace")
    if mode == "replace":
        db.execute("DELETE FROM posts WHERE campaign_id = ?", (campaign_id,))
        db.execute("DELETE FROM tasks WHERE campaign_id = ?", (campaign_id,))
        version = 1
    else:
        version = next_content_version(campaign_id)

    posts = generate_content_for_campaign(campaign)
    for post in posts:
        db.execute(
            """
            INSERT INTO posts (campaign_id, created_at, channel, title, body, status, result, version)
            VALUES (?, ?, ?, ?, ?, 'draft', '', ?)
            """,
            (campaign_id, now_iso(), post["channel"], post["title"], post["body"], version),
        )

    tasks = generate_tasks_for_campaign(campaign)
    for task in tasks:
        db.execute(
            """
            INSERT INTO tasks (campaign_id, created_at, task_date, channel, title, status, result, version)
            VALUES (?, ?, ?, ?, ?, 'todo', '', ?)
            """,
            (campaign_id, now_iso(), task["task_date"], task["channel"], task["title"], version),
        )

    db.commit()
    return redirect(url_for("campaign_view", campaign_id=campaign_id))


@app.post("/admin/campaign/<int:campaign_id>/clear")
@admin_required
def campaign_clear_generated(campaign_id: int):
    db = get_db()
    db.execute("DELETE FROM posts WHERE campaign_id = ?", (campaign_id,))
    db.execute("DELETE FROM tasks WHERE campaign_id = ?", (campaign_id,))
    db.commit()
    return redirect(url_for("campaign_view", campaign_id=campaign_id))


@app.post("/admin/post/<int:post_id>/update")
@admin_required
def post_update(post_id: int):
    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        return redirect(url_for("dashboard"))
    db.execute(
        "UPDATE posts SET status = ?, result = ? WHERE id = ?",
        (request.form.get("status", "draft"), request.form.get("result", ""), post_id),
    )
    db.commit()
    return redirect(url_for("campaign_view", campaign_id=post["campaign_id"]))


@app.post("/admin/task/<int:task_id>/update")
@admin_required
def task_update(task_id: int):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return redirect(url_for("dashboard"))
    db.execute(
        "UPDATE tasks SET status = ?, result = ? WHERE id = ?",
        (request.form.get("status", "todo"), request.form.get("result", ""), task_id),
    )
    db.commit()
    if task["campaign_id"]:
        return redirect(url_for("campaign_view", campaign_id=task["campaign_id"]))
    return redirect(url_for("dashboard"))


@app.route("/admin/leads", methods=["GET", "POST"])
@admin_required
def leads():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            campaign_id_raw = request.form.get("campaign_id", "").strip()
            campaign_id = int(campaign_id_raw) if campaign_id_raw.isdigit() else None
            db.execute(
                """
                INSERT INTO leads (created_at, name, lead_type, municipality, contact, source, campaign_id, status, next_step, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    name,
                    request.form.get("lead_type", "Privatperson"),
                    request.form.get("municipality", ""),
                    request.form.get("contact", ""),
                    request.form.get("source", ""),
                    campaign_id,
                    request.form.get("status", "ny"),
                    request.form.get("next_step", ""),
                    request.form.get("note", ""),
                ),
            )
            db.commit()
        return redirect(url_for("leads"))

    all_leads = db.execute(
        """
        SELECT leads.*, campaigns.name AS campaign_name
        FROM leads
        LEFT JOIN campaigns ON campaigns.id = leads.campaign_id
        ORDER BY leads.id DESC
        """
    ).fetchall()
    campaigns = db.execute("SELECT id, name FROM campaigns ORDER BY id DESC").fetchall()
    return render_template("marketing_leads.html", leads=all_leads, campaigns=campaigns)


@app.post("/admin/lead/<int:lead_id>/update")
@admin_required
def lead_update(lead_id: int):
    db = get_db()
    db.execute(
        "UPDATE leads SET status = ?, next_step = ?, note = ? WHERE id = ?",
        (
            request.form.get("status", "ny"),
            request.form.get("next_step", ""),
            request.form.get("note", ""),
            lead_id,
        ),
    )
    db.commit()
    return redirect(url_for("leads"))


@app.route("/admin/places", methods=["GET", "POST"])
@admin_required
def places():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            db.execute(
                """
                INSERT INTO posting_places (created_at, name, channel, url, area, note, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    name,
                    request.form.get("channel", "Facebook-grupp"),
                    request.form.get("url", "").strip(),
                    request.form.get("area", "").strip(),
                    request.form.get("note", "").strip(),
                    request.form.get("status", "active"),
                ),
            )
            db.commit()
        return redirect(url_for("places"))

    rows = db.execute("SELECT * FROM posting_places ORDER BY status, channel, name").fetchall()
    return render_template("marketing_places.html", places=rows, channels=CHANNELS)


@app.post("/admin/place/<int:place_id>/update")
@admin_required
def place_update(place_id: int):
    db = get_db()
    db.execute(
        "UPDATE posting_places SET status = ?, note = ? WHERE id = ?",
        (request.form.get("status", "active"), request.form.get("note", ""), place_id),
    )
    db.commit()
    return redirect(url_for("places"))


@app.route("/admin/export/leads.csv")
@admin_required
def export_leads():
    db = get_db()
    rows = db.execute(
        """
        SELECT leads.id, leads.created_at, leads.name, leads.lead_type, leads.municipality,
               leads.contact, leads.source, campaigns.name AS campaign, leads.status,
               leads.next_step, leads.note
        FROM leads
        LEFT JOIN campaigns ON campaigns.id = leads.campaign_id
        ORDER BY leads.id DESC
        """
    ).fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "created_at", "name", "type", "municipality", "contact", "source", "campaign", "status", "next_step", "note"])
    for row in rows:
        writer.writerow([row[key] for key in row.keys()])
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=offroad-bumpis-leads.csv"},
    )


@app.route("/go/<code>")
def track_click(code: str):
    db = get_db()
    campaign = db.execute("SELECT * FROM campaigns WHERE code = ?", (clean_code(code),)).fetchone()
    ref = request.args.get("ref", "direct")[:80]
    db.execute(
        """
        INSERT INTO clicks (created_at, campaign_id, code, ref, user_agent)
        VALUES (?, ?, ?, ?, ?)
        """,
        (now_iso(), campaign["id"] if campaign else None, clean_code(code), ref, request.headers.get("User-Agent", "")[:255]),
    )
    db.commit()
    return redirect(campaign["target_url"] if campaign else BOOKING_URL)


@app.route("/qr/<code>.png")
def qr_code(code: str):
    if qrcode is None:
        return Response("qrcode package saknas. Kör pip install -r requirements.txt", status=500)
    ref = request.args.get("ref", "qr")
    link = public_url_for("track_click", code=clean_code(code), ref=ref)
    img = qrcode.make(link)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=f"{clean_code(code)}.png")


@app.template_filter("status_label")
def status_label(value: str) -> str:
    labels = {
        "active": "Aktiv",
        "paused": "Pausad",
        "done": "Klar",
        "draft": "Utkast",
        "posted": "Postad",
        "todo": "Att göra",
        "ny": "Ny",
        "kontaktad": "Kontaktad",
        "svarat": "Svarat",
        "bokad": "Bokad",
        "inte_nu": "Inte nu",
        "död": "Död lead",
        "hidden": "Dold",
    }
    return labels.get(value, value)


if __name__ == "__main__":
    app.run(debug=True)
