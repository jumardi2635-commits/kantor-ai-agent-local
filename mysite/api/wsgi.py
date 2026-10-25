import sys
import os

# Tambahkan path proyek ke sys.path
project_home = '/home/pusatgadaiidn/mysite'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Pastikan direktori api juga ada di path (untuk modul openclaw)
api_path = os.path.join(project_home, 'api')
if api_path not in sys.path:
    sys.path.insert(0, api_path)

# Muat environment variables dari file .env (opsional, jika belum dimuat)
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(project_home, '.env')
    load_dotenv(dotenv_path)
except ImportError:
    pass  # dotenv tidak terinstal, lanjutkan dengan env OS

# Import aplikasi Flask dari app.py
from app import app as application

# Jika perlu, set environment variable tambahan
os.environ.setdefault('FLASK_ENV', 'production')