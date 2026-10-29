import os, secrets
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
import cloudinary, cloudinary.uploader

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
db_url = os.environ.get("DATABASE_URL", "sqlite:///gallery.db")
if db_url.startswith("postgres://"): db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
db = SQLAlchemy(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per hour"], storage_uri="memory://")
ALLOWED = {"jpg", "jpeg", "png", "webp", "gif"}

class Setting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False)
class GalleryImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(255), unique=True, nullable=False)
    image_url = db.Column(db.Text, nullable=False)
    alt_text = db.Column(db.String(255), nullable=False, default="Gallery image")
    created_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

def get_setting(key, default=""):
    item = db.session.get(Setting, key)
    return item.value if item else default
def set_setting(key, value):
    item = db.session.get(Setting, key)
    if item: item.value = value
    else: db.session.add(Setting(key=key, value=value))
    db.session.commit()
def cloud_ready():
    return all(os.environ.get(k) for k in ("CLOUDINARY_CLOUD_NAME","CLOUDINARY_API_KEY","CLOUDINARY_API_SECRET"))
def cloud_setup():
    cloudinary.config(cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"], api_key=os.environ["CLOUDINARY_API_KEY"], api_secret=os.environ["CLOUDINARY_API_SECRET"], secure=True)
def has_admin():
    return bool(get_setting("admin_hash"))
def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"): return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapped
def csrf_check():
    if not secrets.compare_digest(request.form.get("csrf_token",""), session.get("csrf_token","")): abort(400)
@app.after_request
def headers(response):
    response.headers.setdefault("X-Content-Type-Options","nosniff")
    response.headers.setdefault("X-Frame-Options","DENY")
    response.headers.setdefault("Referrer-Policy","strict-origin-when-cross-origin")
    if os.environ.get("RENDER"): response.headers.setdefault("Strict-Transport-Security","max-age=31536000; includeSubDomains")
    return response
@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    flash("Image exceeds the 15 MB limit.","error")
    return redirect(url_for("dashboard"))
@app.route("/")
def home():
    imgs = GalleryImage.query.order_by(GalleryImage.id.asc()).all()
    return render_template("index.html", title=get_setting("title","YOUR TITLE"), images=imgs)
@app.route("/manage-7f3a", methods=["GET"])
def admin_login():
    if session.get("is_admin"): return redirect(url_for("dashboard"))
    if not has_admin(): return render_template("setup.html")
    return render_template("login.html")
@app.route("/manage-7f3a/setup", methods=["POST"])
@limiter.limit("5 per hour")
def setup():
    if has_admin(): abort(404)
    p = request.form.get("password",""); c = request.form.get("confirm_password","")
    if len(p) < 12 or p != c:
        flash("Use at least 12 characters and make both passwords match.","error")
        return redirect(url_for("admin_login"))
    set_setting("admin_hash", generate_password_hash(p))
    session.clear(); session["is_admin"] = True; session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("dashboard"))
@app.route("/manage-7f3a/login", methods=["POST"])
@limiter.limit("5 per minute")
def login():
    if not has_admin(): return redirect(url_for("admin_login"))
    if not check_password_hash(get_setting("admin_hash"), request.form.get("password","")):
        flash("Incorrect password.","error"); return redirect(url_for("admin_login"))
    session.clear(); session["is_admin"] = True; session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("dashboard"))
@app.route("/manage-7f3a/dashboard")
@login_required
def dashboard():
    imgs = GalleryImage.query.order_by(GalleryImage.id.desc()).all()
    return render_template("dashboard.html", title=get_setting("title","YOUR TITLE"), images=imgs, cloud_ready=cloud_ready(), csrf_token=session.get("csrf_token",""))
@app.route("/manage-7f3a/title", methods=["POST"])
@login_required
def update_title():
    csrf_check(); title = request.form.get("title","").strip()
    if not title or len(title) > 100: flash("Title must be 1–100 characters.","error")
    else: set_setting("title",title); flash("Title updated.","success")
    return redirect(url_for("dashboard"))
@app.route("/manage-7f3a/upload", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def upload():
    csrf_check()
    if not cloud_ready(): flash("Cloudinary credentials are not configured in Render yet.","error"); return redirect(url_for("dashboard"))
    f = request.files.get("image")
    if not f or not f.filename: flash("Choose an image first.","error"); return redirect(url_for("dashboard"))
    filename = secure_filename(f.filename)
    ext = filename.rsplit(".",1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED: flash("Use JPG, PNG, WEBP or GIF.","error"); return redirect(url_for("dashboard"))
    try:
        cloud_setup()
        result = cloudinary.uploader.upload(f, folder="persistent-image-gallery", resource_type="image", allowed_formats=list(ALLOWED), transformation=[{"quality":"auto","fetch_format":"auto"}])
        db.session.add(GalleryImage(public_id=result["public_id"], image_url=result["secure_url"], alt_text=(request.form.get("alt_text","").strip() or "Gallery image")[:255]))
        db.session.commit(); flash("Image uploaded.","success")
    except Exception:
        app.logger.exception("Cloudinary upload failed"); flash("Upload failed. Check cloud storage configuration.","error")
    return redirect(url_for("dashboard"))
@app.route("/manage-7f3a/delete/<int:image_id>", methods=["POST"])
@login_required
def delete(image_id):
    csrf_check(); img = db.session.get(GalleryImage,image_id)
    if not img: abort(404)
    try:
        if cloud_ready(): cloud_setup(); cloudinary.uploader.destroy(img.public_id, resource_type="image")
        db.session.delete(img); db.session.commit(); flash("Image deleted.","success")
    except Exception:
        app.logger.exception("Image deletion failed"); flash("Delete failed. Check cloud storage configuration.","error")
    return redirect(url_for("dashboard"))
@app.route("/manage-7f3a/logout", methods=["POST"])
@login_required
def logout():
    csrf_check(); session.clear(); return redirect(url_for("admin_login"))
with app.app_context(): db.create_all()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
