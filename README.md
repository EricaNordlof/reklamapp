# Offroad Bumpis Marketing OS

Intern marknadsföringsapp för Offroad Bumpis.

## Start lokalt

```bash
pip install -r requirements.txt
export ADMIN_PASSWORD=byt_mig
python marketing_app.py
```

Öppna: http://127.0.0.1:5000

## Render

Start command:

```bash
gunicorn marketing_app:app
```

Viktiga miljövariabler:

- `ADMIN_PASSWORD` = ditt adminlösenord
- `SECRET_KEY` = Render kan generera
- `DATA_DIR` = `/opt/render/project/src/data`
- `PUBLIC_BASE_URL` = din publika Render-adress, exempel `https://offroad-bumpis-marketing.onrender.com`
- `BOOKING_URL` = bokningslänk, exempel `https://offroad-bumpis-booking.onrender.com/`

## Filer

- `marketing_app.py`
- `templates/marketing_base.html`
- `templates/marketing_login.html`
- `templates/marketing_dashboard.html`
- `templates/marketing_campaign.html`
- `templates/marketing_leads.html`
- `requirements.txt`
- `render.yaml`
