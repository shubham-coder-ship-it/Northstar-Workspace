"""Loopback-only development runner. Never use this server for deployment."""
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from web import create_app

root = Path(__file__).resolve().parents[1]
folder = root / 'instance'
folder.mkdir(mode=0o700, exist_ok=True)
settings = folder / 'dev-secrets.json'
if not settings.exists():
    data = {'SECRET_KEY':secrets.token_urlsafe(48), 'ENCRYPTION_KEY':Fernet.generate_key().decode()}
    fd = os.open(settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data,stream)
with settings.open() as stream:
    config = json.load(stream)
config.update(DATABASE=str(folder/'practice.sqlite3'),SESSION_COOKIE_SECURE=False, PAPER_TRADING=True, BILLING_LIVE_ENABLED=False,
              PUBLIC_URL=os.environ.get('PUBLIC_URL', 'http://127.0.0.1:5000'))
app=create_app(config)
if __name__ == '__main__':
    app.run(host='127.0.0.1',port=5000,debug=False,use_reloader=False)
