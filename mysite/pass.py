from app import app, db
from models import Admin
with app.app_context():
    admin = Admin.query.filter_by(username='admin').first()
    if not admin:
        admin = Admin(username='admin')
        db.session.add(admin)
    admin.set_password('slebor777')
    db.session.commit()