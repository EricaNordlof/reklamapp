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


def generate_content_for_campaign(campaign: sqlite3.Row) -> list[dict[str, str]]:
    c = dict(campaign)
    area = c["area"].strip() or "Skåne"
    offer = c["offer"].strip() or "Boka datum nu och betala senast 3 dagar före utlämning."
    product = c["product"].strip() or "Bumperballs"
    audience = c["audience"].strip() or "lokala kunder"

    fb_link = tracking_url(c, "facebook-grupp")
    insta_link = tracking_url(c, "instagram")
    gbp_link = tracking_url(c, "google-business")
    reddit_link = tracking_url(c, "reddit")
    mail_link = tracking_url(c, "mail")
    flyer_link = tracking_url(c, "flyer")
    seo_link = tracking_url(c, "seo")

    return [
        {
            "channel": "Facebook-grupp",
            "title": f"FB-grupp: {area}",
            "body": f"""Tips till er som vill göra något roligt med barnen i {area} 👇

Jag hyr ut {product.lower()} via Offroad Bumpis. Det passar bra till barnkalas, skolavslutning, fritids, föreningar och kompisgäng.

Det är stora uppblåsbara bollar som man springer runt i, krockar lite lagom och ramlar utan att det gör ont. Enkelt upplägg, batteridriven pump och det funkar på gräs, konstgräs eller i idrottshall.

{offer}

Boka här:
{fb_link}

#offroadbumpis #bumperballs #barnkalas #skåne""",
        },
        {
            "channel": "Instagram",
            "title": f"Instagram: {campaign['name']}",
            "body": f"""Bumperballs i {area} ⚽️💥

Perfekt när man vill ha en aktivitet som barnen faktiskt minns efteråt.

✅ {product}
✅ För kalas, skolor, fritids och föreningar
✅ Batteridriven pump
✅ Kan spelas på gräs, konstgräs eller i hall
✅ {offer}

Boka via länken:
{insta_link}

#offroadbumpis #bumperballs #barnkalas #skåne #malmö #lund #trelleborg #helsingborg #aktivitetförbarn""",
        },
        {
            "channel": "Facebook-sida",
            "title": "Facebook-sida: kampanjpost",
            "body": f"""Ny kampanj: {campaign['name']}

Offroad Bumpis hyr ut {product.lower()} i {area}. Passar för {audience.lower()}.

{offer}

Vill du boka ett datum? Gå hit:
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
            "body": f"""Ämne: Bumperballs till aktivitet i {area}

Hej!

Jag heter Erica och driver Offroad Bumpis i Skåne.

Jag hyr ut {product.lower()} till bland annat skolor, fritids, föreningar, kalas och gruppaktiviteter. Det är en enkel aktivitet där deltagarna springer runt i stora uppblåsbara bollar och spelar/krockar på ett kontrollerat sätt.

Kort upplägg:
- passar bäst på gräs, konstgräs eller i idrottshall
- batteridriven pump ingår
- tydliga priser
- leverans kan bokas inom Skåne
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
Hyr bumperballs i {area} | Offroad Bumpis

Meta description:
Hyr bumperballs i {area} till barnkalas, skola, fritids, förening eller event. Enkel bokning, tydliga priser och leverans inom Skåne.

Rubrik:
Bumperballs i {area}

Textstart:
Vill du hyra bumperballs i {area}? Offroad Bumpis hyr ut {product.lower()} till kalas, skolor, fritids, föreningar och andra gruppaktiviteter. Det funkar bäst på gräs, konstgräs eller i idrottshall.

CTA:
Boka här: {seo_link}""",
        },
    ]


def generate_tasks_for_campaign(campaign: sqlite3.Row) -> list[dict[str, str]]:
    start = date.today()
    area = campaign["area"] or "Skåne"
    return [
        {"task_date": (start + timedelta(days=0)).isoformat(), "channel": "Facebook-grupp", "title": f"Dela kampanjen i 2 relevanta grupper för {area}."},
        {"task_date": (start + timedelta(days=1)).isoformat(), "channel": "Instagram", "title": "Lägg upp bild/reel + använd Instagramtexten."},
        {"task_date": (start + timedelta(days=2)).isoformat(), "channel": "Google Business Profile", "title": "Publicera lokalt Google-inlägg."},
        {"task_date": (start + timedelta(days=3)).isoformat(), "channel": "Mail", "title": "Skicka mailet till 5 skolor/föreningar/företag."},
        {"task_date": (start + timedelta(days=4)).isoformat(), "channel": "Flyer", "title": "Lägg flyers på 1–2 platser nära park/café/hall."},
        {"task_date": (start + timedelta(days=5)).isoformat(), "channel": "Lead", "title": "Följ upp alla som svarat, gillat eller frågat."},
        {"task_date": (start + timedelta(days=6)).isoformat(), "channel": "Analys", "title": "Kolla vad som gav klick/bokning. Skrota resten utan sentimentalitet."},
    ]


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

    return render_template(
        "marketing_dashboard.html",
        stats=stats,
        campaigns=campaigns,
        tasks=tasks,
        leads=leads,
        clicks=clicks,
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

    posts = db.execute("SELECT * FROM posts WHERE campaign_id = ? ORDER BY id DESC", (campaign_id,)).fetchall()
    tasks = db.execute("SELECT * FROM tasks WHERE campaign_id = ? ORDER BY task_date ASC, id ASC", (campaign_id,)).fetchall()
    leads = db.execute("SELECT * FROM leads WHERE campaign_id = ? ORDER BY id DESC", (campaign_id,)).fetchall()
    clicks = db.execute(
        "SELECT ref, COUNT(*) AS total FROM clicks WHERE campaign_id = ? GROUP BY ref ORDER BY total DESC",
        (campaign_id,),
    ).fetchall()

    tracking_links = [
        {"label": "Facebook-grupp", "url": tracking_url(campaign, "facebook-grupp")},
        {"label": "Instagram", "url": tracking_url(campaign, "instagram")},
        {"label": "Google Business", "url": tracking_url(campaign, "google-business")},
        {"label": "Mail", "url": tracking_url(campaign, "mail")},
        {"label": "Flyer", "url": tracking_url(campaign, "flyer")},
    ]

    return render_template(
        "marketing_campaign.html",
        campaign=campaign,
        posts=posts,
        tasks=tasks,
        leads=leads,
        clicks=clicks,
        tracking_links=tracking_links,
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

    posts = generate_content_for_campaign(campaign)
    for post in posts:
        db.execute(
            """
            INSERT INTO posts (campaign_id, created_at, channel, title, body, status, result)
            VALUES (?, ?, ?, ?, ?, 'draft', '')
            """,
            (campaign_id, now_iso(), post["channel"], post["title"], post["body"]),
        )

    tasks = generate_tasks_for_campaign(campaign)
    for task in tasks:
        db.execute(
            """
            INSERT INTO tasks (campaign_id, created_at, task_date, channel, title, status, result)
            VALUES (?, ?, ?, ?, ?, 'todo', '')
            """,
            (campaign_id, now_iso(), task["task_date"], task["channel"], task["title"]),
        )

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
    }
    return labels.get(value, value)


if __name__ == "__main__":
    app.run(debug=True)
